"""Single entry point for all Instagram access. Callers use get_provider()
and IGProvider.fetch_profile() only — swapping the underlying data source
(Apify or Instaloader) never touches signal code."""

import json
import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from itertools import islice

from shared.config import settings
from shared.schemas import IGComment, IGPost, IGProfile, IGTag

logger = logging.getLogger(__name__)

USD_TO_INR = 83.0
PROFILE_ACTOR = "apify/instagram-profile-scraper"
DETAILS_ACTOR = "apify/instagram-scraper"


class IGProviderError(Exception):
    pass


class IGProvider(ABC):
    @abstractmethod
    def fetch_profile(self, handle: str) -> tuple[IGProfile, float]:
        """Returns (profile, cost_inr)."""


class ApifyIGProvider(IGProvider):
    def __init__(self, token: str | None = None):
        from apify_client import ApifyClient

        self.client = ApifyClient(token or settings.APIFY_TOKEN)

    def fetch_profile(self, handle: str) -> tuple[IGProfile, float]:
        handle = handle.lower().lstrip("@")
        cost_inr = 0.0

        profile_run = self.client.actor(PROFILE_ACTOR).call(logger=None, run_input={"usernames": [handle]})
        if profile_run is None:
            raise IGProviderError(f"Apify profile scraper run did not complete for @{handle}")
        cost_inr += self._run_cost_inr(profile_run)
        items = list(self.client.dataset(profile_run.default_dataset_id).iterate_items())
        if not items:
            raise IGProviderError(f"Apify profile scrape returned no data for @{handle}")
        item = items[0]

        recent_posts = [self._to_post(p) for p in (item.get("latestPosts") or [])[:12]]

        profile = IGProfile(
            handle=handle,
            ig_user_id=str(item.get("id") or item.get("userId") or "") or None,
            display_name=item.get("fullName"),
            bio_text=item.get("biography"),
            bio_website_url=item.get("externalUrl") or (item.get("externalUrls") or [None])[0],
            follower_count=item.get("followersCount"),
            following_count=item.get("followsCount"),
            post_count=item.get("postsCount"),
            is_private=bool(item.get("private", False)),
            recent_posts=recent_posts,
            oldest_post_at=min((p.posted_at for p in recent_posts if p.posted_at), default=None),
            source="apify",
            raw={"profile": item},
        )

        # The profile scrape's latestPosts carry real per-post commentsCount --
        # that's the source of truth for "comments disabled". (A separate
        # resultsType=details run returns a profile object with no counts, which
        # previously false-positived this for every seller.)
        profile.comments_disabled = bool(recent_posts) and all((p.comment_count or 0) == 0 for p in recent_posts)

        # Comment TEXT is never inlined by the scrapers -- posts come back with
        # latestComments empty even when commentsCount is high. Fetch it for the
        # seller's own most-commented posts (signal 4).
        own_post_urls = [
            f"https://www.instagram.com/p/{p.shortcode}/"
            for p in sorted(recent_posts, key=lambda p: p.comment_count or 0, reverse=True)
            if p.shortcode and (p.comment_count or 0) > 0
        ][: self.COMMENT_POSTS_PER_RUN]
        profile.comments, own_comments_cost = self._fetch_comments(own_post_urls)
        cost_inr += own_comments_cost

        # "mentions" = posts made by OTHER accounts that tag this seller (the
        # profile's "Tagged" tab) -- the organic customer-proof signal 3 needs.
        try:
            mentions_run = self.client.actor(DETAILS_ACTOR).call(
                logger=None,
                run_input={
                    "directUrls": [f"https://www.instagram.com/{handle}/"],
                    "resultsType": "mentions",
                    "resultsLimit": 8,
                    "addParentData": True,
                }
            )
            cost_inr += self._run_cost_inr(mentions_run)
            mention_items = list(self.client.dataset(mentions_run.default_dataset_id).iterate_items())
            profile.tagged_posts, _ = self._extract_mentions(mention_items)

            # Comments on tagged posts live on OTHER accounts, so the seller
            # can't curate them away -- the least gameable feedback we have.
            tagged_urls = [
                p.get("url")
                for p in sorted(mention_items, key=lambda p: p.get("commentsCount") or 0, reverse=True)
                if p.get("url") and (p.get("commentsCount") or 0) > 0
            ][: self.COMMENT_POSTS_PER_RUN]
            profile.tagged_post_comments, tagged_comments_cost = self._fetch_comments(tagged_urls)
            cost_inr += tagged_comments_cost
        except Exception as exc:
            logger.warning("Apify mentions run failed for @%s, degrading gracefully: %s", handle, exc)

        return profile, cost_inr

    COMMENT_POSTS_PER_RUN = 3  # posts to pull comments from, most-commented first
    COMMENTS_PER_POST = 10

    def _fetch_comments(self, post_urls: list[str]) -> tuple[list[IGComment], float]:
        if not post_urls:
            return [], 0.0
        try:
            comments_run = self.client.actor(DETAILS_ACTOR).call(
                logger=None,
                run_input={
                    "directUrls": post_urls,
                    "resultsType": "comments",
                    "resultsLimit": self.COMMENTS_PER_POST,
                }
            )
            cost = self._run_cost_inr(comments_run)
            items = list(self.client.dataset(comments_run.default_dataset_id).iterate_items())
            comments = [
                IGComment(
                    post_id=str(c.get("postId") or ""),
                    author_username=c.get("ownerUsername"),
                    text=c.get("text", ""),
                    posted_at=self._parse_timestamp(c.get("timestamp")),
                )
                for c in items
                if c.get("text")
            ]
            return comments, cost
        except Exception as exc:
            logger.warning("Apify comments run failed, degrading gracefully: %s", exc)
            return [], 0.0

    @staticmethod
    def _parse_timestamp(ts) -> datetime | None:
        if not ts:
            return None
        try:
            return (
                datetime.fromtimestamp(int(ts), tz=timezone.utc)
                if isinstance(ts, (int, float))
                else datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            )
        except (ValueError, TypeError):
            return None

    @classmethod
    def _to_post(cls, p: dict) -> IGPost:
        return IGPost(
            id=str(p.get("id") or ""),
            shortcode=p.get("shortCode") or p.get("shortcode"),
            image_url=p.get("displayUrl") or p.get("imageUrl"),
            caption=p.get("caption"),
            posted_at=cls._parse_timestamp(p.get("timestamp") or p.get("takenAtTimestamp")),
            like_count=p.get("likesCount"),
            comment_count=p.get("commentsCount"),
        )

    @classmethod
    def _extract_mentions(cls, mention_items: list[dict]) -> tuple[list[IGTag], list[IGComment]]:
        """mention_items are posts owned by OTHER accounts that tag this seller."""
        tags: list[IGTag] = []
        comments: list[IGComment] = []
        for post in mention_items:
            post_id = str(post.get("id") or "")
            tags.append(
                IGTag(
                    post_id=post_id,
                    tagger_username=post.get("ownerUsername"),
                    posted_at=cls._parse_timestamp(post.get("timestamp") or post.get("takenAtTimestamp")),
                )
            )
            for c in (post.get("latestComments") or post.get("comments") or [])[:10]:
                comments.append(
                    IGComment(
                        post_id=post_id,
                        author_username=c.get("ownerUsername") or c.get("username"),
                        text=c.get("text", ""),
                    )
                )
        return tags, comments

    @staticmethod
    def _run_cost_inr(run) -> float:
        usage_usd = run.usage_total_usd
        if usage_usd is None:
            usage_usd = 0.5  # flat estimate when the run object doesn't expose usage
        return float(usage_usd) * USD_TO_INR


class InstaloaderIGProvider(IGProvider):
    """Free local scraper using the instaloader library. No API costs.

    Rate-limit mitigation: delays between requests, optional session cookie.
    Works for public profiles only — private profiles raise IGProviderError.
    """

    INTER_REQUEST_DELAY = 2.0  # seconds between Instagram requests
    MAX_POSTS = 12
    MAX_TAGGED = 8
    COMMENT_POSTS_PER_RUN = 3
    COMMENTS_PER_POST = 10

    def __init__(self):
        import instaloader

        self._L = instaloader.Instaloader(
            download_pictures=False,
            download_videos=False,
            download_video_thumbnails=False,
            download_geotags=False,
            download_comments=False,
            save_metadata=False,
            compress_json=False,
            quiet=True,
        )
        if settings.IG_SESSION_ID:
            try:
                self._L.context._session.cookies.set(
                    "sessionid", settings.IG_SESSION_ID,
                    domain=".instagram.com", path="/", secure=True,
                )
                logger.info("Instaloader: using session cookie for authenticated access")
            except Exception as exc:
                logger.warning("Instaloader: session cookie setup failed: %s", exc)

    def _delay(self):
        time.sleep(self.INTER_REQUEST_DELAY)

    def fetch_profile(self, handle: str) -> tuple[IGProfile, float]:
        import instaloader

        handle = handle.lower().lstrip("@")

        try:
            ig_profile = instaloader.Profile.from_username(self._L.context, handle)
        except instaloader.ProfileNotExistsException:
            raise IGProviderError(f"Instagram profile @{handle} does not exist")
        except instaloader.ConnectionException as exc:
            raise IGProviderError(f"Instagram connection error for @{handle}: {exc}")
        except Exception as exc:
            raise IGProviderError(f"Failed to fetch @{handle}: {exc}")

        if ig_profile.is_private:
            return IGProfile(
                handle=handle,
                ig_user_id=str(ig_profile.userid),
                display_name=ig_profile.full_name,
                bio_text=ig_profile.biography,
                bio_website_url=ig_profile.external_url,
                follower_count=ig_profile.followers,
                following_count=ig_profile.followees,
                post_count=ig_profile.mediacount,
                is_private=True,
                source="instaloader",
                raw={},
            ), 0.0

        recent_posts: list[IGPost] = []
        post_objects = []
        try:
            for post in islice(ig_profile.get_posts(), self.MAX_POSTS):
                recent_posts.append(IGPost(
                    id=str(post.mediaid),
                    shortcode=post.shortcode,
                    image_url=post.url,
                    caption=post.caption,
                    posted_at=post.date_utc.replace(tzinfo=timezone.utc) if post.date_utc else None,
                    like_count=post.likes,
                    comment_count=post.comments,
                ))
                post_objects.append(post)
        except Exception as exc:
            logger.warning("Instaloader: error fetching posts for @%s: %s", handle, exc)

        oldest_post_at = min(
            (p.posted_at for p in recent_posts if p.posted_at), default=None
        )

        comments_disabled = (
            bool(recent_posts) and
            all((p.comment_count or 0) == 0 for p in recent_posts)
        )

        self._delay()

        # Fetch comments from the most-commented own posts
        comments = self._fetch_comments_from_posts(post_objects)

        self._delay()

        # Fetch tagged posts (posts by others tagging this seller)
        tagged_posts, tagged_post_comments = self._fetch_tagged(ig_profile)

        profile = IGProfile(
            handle=handle,
            ig_user_id=str(ig_profile.userid),
            display_name=ig_profile.full_name,
            bio_text=ig_profile.biography,
            bio_website_url=ig_profile.external_url,
            follower_count=ig_profile.followers,
            following_count=ig_profile.followees,
            post_count=ig_profile.mediacount,
            is_private=False,
            comments_disabled=comments_disabled,
            oldest_post_at=oldest_post_at,
            recent_posts=recent_posts,
            comments=comments,
            tagged_posts=tagged_posts,
            tagged_post_comments=tagged_post_comments,
            source="instaloader",
            raw={},
        )

        return profile, 0.0

    def _fetch_comments_from_posts(self, post_objects: list) -> list[IGComment]:
        sorted_posts = sorted(
            post_objects,
            key=lambda p: p.comments or 0,
            reverse=True,
        )
        target_posts = [
            p for p in sorted_posts if (p.comments or 0) > 0
        ][:self.COMMENT_POSTS_PER_RUN]

        comments: list[IGComment] = []
        for post in target_posts:
            try:
                for i, comment in enumerate(post.get_comments()):
                    if i >= self.COMMENTS_PER_POST:
                        break
                    comments.append(IGComment(
                        post_id=str(post.mediaid),
                        author_username=comment.owner.username if comment.owner else None,
                        text=comment.text,
                        posted_at=(
                            comment.created_at_utc.replace(tzinfo=timezone.utc)
                            if comment.created_at_utc else None
                        ),
                    ))
                self._delay()
            except Exception as exc:
                logger.warning("Instaloader: comment fetch failed for post %s: %s", post.shortcode, exc)
        return comments

    def _fetch_tagged(self, ig_profile) -> tuple[list[IGTag], list[IGComment]]:
        tagged_posts: list[IGTag] = []
        tagged_post_objects = []

        try:
            for post in islice(ig_profile.get_tagged_posts(), self.MAX_TAGGED):
                tagged_posts.append(IGTag(
                    post_id=str(post.mediaid),
                    tagger_username=post.owner_username,
                    posted_at=(
                        post.date_utc.replace(tzinfo=timezone.utc)
                        if post.date_utc else None
                    ),
                ))
                tagged_post_objects.append(post)
        except Exception as exc:
            logger.warning("Instaloader: tagged posts fetch failed: %s", exc)

        self._delay()

        tagged_comments = self._fetch_comments_from_posts(tagged_post_objects)
        return tagged_posts, tagged_comments


class ScreenshotVisionIGProvider(IGProvider):
    """Fallback path when Apify is unavailable/blocked: user forwards a
    screenshot instead of a handle, a vision LLM reads what's on screen."""

    EXTRACTION_PROMPT = (
        "Extract what is visible in this Instagram profile screenshot. "
        "Return ONLY a compact JSON object with keys: handle, display_name, "
        "bio_text, follower_count, following_count, post_count. "
        "Use null for anything not visible. Do not guess or invent numbers."
    )

    def __init__(self, api_key: str | None = None):
        pass  # provider resolved per-call via engine.llm.get_pii_llm()

    def fetch_profile(self, handle: str) -> tuple[IGProfile, float]:
        raise IGProviderError("ScreenshotVisionIGProvider needs an image — call extract_from_screenshot()")

    def extract_from_screenshot(self, image_bytes: bytes, media_type: str = "image/jpeg") -> tuple[IGProfile, float]:
        # User-submitted screenshots can capture personal context (notification
        # bar, DMs) -> treated as PII, routed off the free tier.
        from engine.llm import get_pii_llm

        text, cost_inr = get_pii_llm().complete_vision(self.EXTRACTION_PROMPT, image_bytes, media_type, max_tokens=400)
        data = self._parse_json(text)

        profile = IGProfile(
            handle=(data.get("handle") or "").lower().lstrip("@") or "unknown",
            display_name=data.get("display_name"),
            bio_text=data.get("bio_text"),
            follower_count=data.get("follower_count"),
            following_count=data.get("following_count"),
            post_count=data.get("post_count"),
            source="screenshot_vision",
            raw={"vision_extraction": data},
        )
        return profile, cost_inr

    @staticmethod
    def _parse_json(text: str) -> dict:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            return {}
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return {}

def get_provider() -> IGProvider:
    if settings.IG_PROVIDER == "apify":
        return ApifyIGProvider()
    return InstaloaderIGProvider()
