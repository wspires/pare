import { getJson } from "./client";
import { Session } from "./session";

export async function login(user: string, password: string): Promise<Session> {
  const result = await getJson<{ token: string }>(`/login?u=${user}&p=${password}`);
  if (result.ok) {
    return { token: result.data.token, user };
  }
  throw new Error(result.error);
}
