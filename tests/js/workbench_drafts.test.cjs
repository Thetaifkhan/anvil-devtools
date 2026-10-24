// Unit tests for static/js/workbench-drafts.js (draft persistence policy).
// The storage object is faked, so these run without a browser.
const test = require("node:test");
const assert = require("node:assert/strict");
const path = require("node:path");

const drafts = require(path.join(__dirname, "..", "..", "static", "js", "workbench-drafts.js"));

// Minimal Storage stand-in. `failOn` makes the named method throw.
function fakeStorage(failOn = []) {
  const data = new Map();
  const maybeFail = (name) => { if (failOn.includes(name)) throw new Error(`${name} blocked`); };
  return {
    data,
    getItem(k) { maybeFail("getItem"); return data.has(k) ? data.get(k) : null; },
    setItem(k, v) { maybeFail("setItem"); data.set(k, String(v)); },
    removeItem(k) { maybeFail("removeItem"); data.delete(k); },
  };
}

test("save then load round-trips input drafts", () => {
  const storage = fakeStorage();
  const store = drafts.createDraftStore(storage);
  assert.equal(store.save({ formatter: { a: "<root/>", b: "" }, diff: { a: "1", b: "2" } }), true);
  assert.deepEqual(store.load(), { formatter: { a: "<root/>", b: "" }, diff: { a: "1", b: "2" } });
});

test("stored payload is versioned", () => {
  const storage = fakeStorage();
  drafts.createDraftStore(storage).save({ formatter: { a: "x", b: "" } });
  const parsed = JSON.parse(storage.data.get(drafts.STORAGE_KEY));
  assert.equal(parsed.v, 1);
  assert.equal(parsed.v, drafts.VERSION);
});

test("empty drafts are not written and an empty map removes the key", () => {
  const storage = fakeStorage();
  const store = drafts.createDraftStore(storage);
  store.save({ formatter: { a: "x", b: "" } });
  assert.ok(storage.data.has(drafts.STORAGE_KEY));
  store.save({ formatter: { a: "", b: "" } });
  assert.equal(storage.data.has(drafts.STORAGE_KEY), false);
});

test("JWT slot is never persisted, even if a caller passes it", () => {
  const storage = fakeStorage();
  const store = drafts.createDraftStore(storage);
  store.save({ jwt: { a: "eyJhbGciOi...", b: "" }, formatter: { a: "x", b: "" } });
  const parsed = JSON.parse(storage.data.get(drafts.STORAGE_KEY));
  assert.deepEqual(Object.keys(parsed.drafts), ["formatter"]);
  assert.equal(JSON.stringify(parsed).includes("eyJ"), false);
});

test("malformed JSON is ignored and cleared instead of throwing", () => {
  const storage = fakeStorage();
  storage.data.set(drafts.STORAGE_KEY, "{not json");
  const store = drafts.createDraftStore(storage);
  assert.deepEqual(store.load(), {});
  assert.equal(storage.data.has(drafts.STORAGE_KEY), false);
});

test("outdated or foreign schema versions are discarded", () => {
  for (const payload of [
    { v: 2, drafts: { formatter: { a: "x", b: "" } } },
    { drafts: { formatter: { a: "x", b: "" } } },
    { v: 1, drafts: [] },
    [1, 2, 3],
    null,
    "string",
  ]) {
    const storage = fakeStorage();
    storage.data.set(drafts.STORAGE_KEY, JSON.stringify(payload));
    assert.deepEqual(drafts.createDraftStore(storage).load(), {}, JSON.stringify(payload));
  }
});

test("invalid slots, non-string fields and oversize text are dropped individually", () => {
  const storage = fakeStorage();
  const big = "x".repeat(drafts.MAX_CHARS_PER_FIELD + 1);
  storage.data.set(drafts.STORAGE_KEY, JSON.stringify({
    v: 1,
    drafts: {
      "../evil": { a: "x", b: "" },
      "UPPER": { a: "x", b: "" },
      formatter: { a: 42, b: "ok" },
      validator: { a: big, b: "" },
      converter: null,
      encoder: { a: "kept", b: "" },
    },
  }));
  assert.deepEqual(drafts.createDraftStore(storage).load(), {
    formatter: { a: "", b: "ok" },
    encoder: { a: "kept", b: "" },
  });
});

test("JWT entries already in storage are ignored on load", () => {
  const storage = fakeStorage();
  storage.data.set(drafts.STORAGE_KEY, JSON.stringify({ v: 1, drafts: { jwt: { a: "eyJ", b: "" } } }));
  assert.deepEqual(drafts.createDraftStore(storage).load(), {});
});

test("blocked storage (null) degrades to in-memory behaviour without throwing", () => {
  const store = drafts.createDraftStore(null);
  assert.equal(store.available(), false);
  assert.deepEqual(store.load(), {});
  assert.equal(store.save({ formatter: { a: "x", b: "" } }), false);
  assert.equal(store.discard(), true);
});

test("throwing storage (quota, security errors) never propagates", () => {
  for (const method of ["getItem", "setItem", "removeItem"]) {
    const store = drafts.createDraftStore(fakeStorage([method]));
    assert.doesNotThrow(() => store.available());
    assert.doesNotThrow(() => store.load());
    assert.doesNotThrow(() => store.save({ formatter: { a: "x", b: "" } }));
    assert.doesNotThrow(() => store.discard());
  }
});

test("available() reports false when writes do not round-trip", () => {
  assert.equal(drafts.createDraftStore(fakeStorage(["setItem"])).available(), false);
  assert.equal(drafts.createDraftStore(fakeStorage()).available(), true);
});

test("discard() removes the stored draft and leaves other keys alone", () => {
  const storage = fakeStorage();
  storage.data.set("unrelated.key", "keep");
  const store = drafts.createDraftStore(storage);
  store.save({ formatter: { a: "x", b: "" } });
  assert.equal(store.discard(), true);
  assert.equal(storage.data.has(drafts.STORAGE_KEY), false);
  assert.equal(storage.data.get("unrelated.key"), "keep");
});
