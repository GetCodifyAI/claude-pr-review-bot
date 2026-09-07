import assert from "node:assert/strict";
import { test } from "node:test";
import { prnum } from "./pr";

test("bare number", () => assert.equal(prnum("38849"), "38849"));
test("hash prefix", () => assert.equal(prnum("#38849"), "38849"));
test("trims whitespace", () => assert.equal(prnum("  123 "), "123"));
test("github pull URL", () =>
  assert.equal(prnum("https://github.com/GetCodifyAI/cut-and-dry/pull/39190"), "39190"));
test("URL wins over trailing text", () =>
  assert.equal(prnum("see /pull/42 please"), "42"));
test("non-numeric is empty", () => assert.equal(prnum("hello"), ""));
test("empty string is empty", () => assert.equal(prnum(""), ""));
test("too long is empty", () => assert.equal(prnum("12345678"), ""));
