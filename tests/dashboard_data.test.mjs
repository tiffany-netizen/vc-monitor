import test from "node:test";
import assert from "node:assert/strict";

import { getJobsEmptyState, supaFetch } from "../dashboard-data.mjs";

test("supaFetch throws a useful error on non-2xx responses", async () => {
  const fetchImpl = async () => ({
    ok: false,
    status: 503,
    text: async () => "service unavailable",
  });

  await assert.rejects(
    () => supaFetch("/jobs?select=*", { fetchImpl, baseUrl: "https://example.supabase.co/rest/v1", apiKey: "test" }),
    /Supabase request failed \(503\) for \/jobs\?select=\*: service unavailable/
  );
});

test("getJobsEmptyState returns the empty message only when there are no jobs", () => {
  assert.equal(getJobsEmptyState([]), "No jobs tracked yet.");
  assert.equal(getJobsEmptyState([{ id: 1, title: "VP of Operations" }]), null);
});