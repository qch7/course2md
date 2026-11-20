import { expect, it } from "vitest";
import type { Job } from "@/api/client";

import { store } from "./jobs";
import { queryClient } from "./query";
import { options, resetSession, session } from "./session";

it("clears account data, query results and private options together", () => {
  session.user = { id: "a", username: "Alice", role: "admin", disabled: false };
  queryClient.setQueryData(["users"], ["Alice"]);
  options.agentChoices.secret = { provider: "private", model: "private" };
  options.outDir = "private-directory";
  store.jobs.set("a", { id: "a" } as Job);
  resetSession();
  expect(session.user).toBeNull();
  expect(session.config).toBeNull();
  expect(queryClient.getQueryData(["users"])).toBeUndefined();
  expect(options.agentChoices).toEqual({});
  expect(options.outDir).toBe("");
  expect(store.jobs.size).toBe(0);
});
