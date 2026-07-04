import { API_URL } from "../../../../../lib/api";

export async function GET(_request, { params }) {
  const { handle } = await params;
  if (!/^[a-zA-Z0-9._]{1,30}$/.test(handle)) {
    return Response.json({ error: "bad handle" }, { status: 400 });
  }
  const res = await fetch(`${API_URL}/api/sellers/${handle}`, { cache: "no-store" });
  if (res.status === 404) return Response.json({ error: "not checked" }, { status: 404 });
  const data = await res.json();
  return Response.json(data);
}
