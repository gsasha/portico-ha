// The kiosk page's house calls (M7 3d, design 15.14): the Everyone key, the outgoing overlay (calling, who answered, the
// outcomes), an incoming call from a phone (answer / decline), and above all the GATE: while the server's house-call model
// is off the roster has no `everyone` entry and the page is byte-identical to what it was before 3d (golden hashes taken
// from the card at origin/main before this change). jsdom has no layout engine: the arithmetic is pure functions tested
// with numbers, the DOM as text. NOT a visual check (see test_card_visual.html ?wide-everyone). No audio here (3b).
import assert from "assert";
import crypto from "crypto";
import { loadCard, sleep, makeHass, CONTENT, DOOR_AUDIO } from "./card_loader.mjs";

const { dom, PorticoCard } = loadCard();
const doc = dom.window.document;
const sha = (t) => crypto.createHash("sha256").update(t).digest("hex").slice(0, 16);
const P = PorticoCard;

const cam = (id) => ({ id, label: id.toUpperCase(), entity_id: `camera.${id}`, size: "big", available: true, name: id });
const act = (id) => ({ id, label: `L-${id}`, kind: "script_run", entity_id: `script.${id}`, confirm: false, available: true, name: id });
const PHONES = [
  { action_id: "r-alice", id: "ep-alice", kind: "phone", name: "Alice", available: true, reason: null },
  { action_id: "r-dina", id: "ep-dina", kind: "phone", name: "Dina", available: true, reason: null },
];
const EVERYONE = { action_id: "r-every", id: "everyone", kind: "group", name: "Everyone", available: true, reason: null, count: 2 };
const kitchen = (roster) => ({ ...CONTENT, endpoint: "ep-kitchen", kind: "dashboard", roster,
  pages: [{ id: "home", label: "Home", blocks: [
    { id: "b1", title: "Gate", cameras: [cam("a"), cam("b"), cam("c")], actions: [act("x"), act("y")] },
    { id: "b2", title: "Yard", cameras: [cam("d")], actions: [act("z")] }] }] });
const q = (card, sel) => card.shadowRoot.querySelector(sel);
const overlay = () => doc.querySelector("[data-portico-house]");
const oq = (sel) => overlay() && overlay().shadowRoot.querySelector(sel);
const otext = () => (overlay() ? overlay().shadowRoot.textContent : "");

async function mount(roster, box, opts = {}, config = {}) {
  const fake = makeHass({ doors: [], content: kitchen(roster), ...opts });
  const card = doc.createElement("portico-card");
  doc.body.appendChild(card);
  card.setConfig({ _pollMs: 20, _snapshotMs: 30, endpoint: "ep-kitchen", fullpage: true, ...config });
  if (box) card._box = { w: box[0], h: box[1] };
  card.hass = fake.hass;
  await sleep(60);
  return { card, fake };
}
const hc = (state, extra) => ({ type: "house_call", call_id: "hso-1", state, from: "ep-kitchen", to: "everyone", ...extra });
const incoming = (state, extra) => ({ type: "house_call", call_id: "hso-9", state, from: "ep-alice", to: "ep-kitchen", ...extra });

async function main() {
  // 1. THE GATE: house calls off = no key in the roster = the page is byte-identical to the card before 3d (golden hashes, re-pinned for the scrolling kiosk page on the main screen background, 2026-11-12; earlier for live video: the tiles gained the tap/live attributes and the card the live CSS)
  {
    const golden = {
      "1280x800": "cd769c88155e34bd", "1920x1200": "b7cd3403fd235019", "1024x600": "a47ef1e41c561df0", "800x480": "80d10b5910c50735" };
    for (const [size, hash] of Object.entries(golden)) {
      const [w, h] = size.split("x").map(Number);
      for (const roster of [PHONES, []]) {
        const { card } = await mount(roster, [w, h]);
        assert.strictEqual(sha(card.shadowRoot.innerHTML), hash, `${size}: the wide page changed while house calls are off`);
        assert.ok(!q(card, "#everyone-btn") && !q(card, ".house-row"), "no key");
        assert.ok(!overlay(), "no house overlay element");
        assert.ok(!card.shadowRoot.innerHTML.includes("house"), "not even a style rule mentions it");
        card.remove();
      }
    }
    const narrow = await mount(PHONES, [500, 900]);
    assert.strictEqual(sha(narrow.card.shadowRoot.innerHTML), "03799e853f7ca3d6", "narrow fullpage page: only the kiosk background and scroll style changed (2026-11-12)");
    narrow.card.remove();
    const plain = await mount(PHONES, [500, 900], {}, { fullpage: false });
    assert.strictEqual(sha(plain.card.shadowRoot.innerHTML), "a111f935b7941c10", "narrow non-fullpage page unchanged");
    plain.card.remove();
    const lovelace = await mount(PHONES, [1280, 800], {}, { fullpage: false });
    assert.strictEqual(sha(lovelace.card.shadowRoot.innerHTML), "6b0b9871783efe6d", "wide Lovelace page: only fullpage-only rules left it (selectors that never matched a card), 2026-11-12");
    lovelace.card.remove();
    console.log("PASS: GATE: with no `everyone` in the roster the page is byte-identical to before 3d (4 wide sizes x 2 rosters, narrow, Lovelace), no key, no overlay, no style");
  }

  // 2. the key is drawn only when the server sent it, only on a fullpage card
  {
    const on = await mount([EVERYONE, ...PHONES], [1280, 800]);
    const b = q(on.card, "#everyone-btn");
    assert.ok(b, "the key is drawn when the roster carries Everyone");
    assert.ok(b.textContent.includes("Everyone (2 phones)"), b.textContent);
    assert.ok(!b.disabled);
    assert.ok(q(on.card, "#back-rail"), "the back rail is still there");
    const rows = on.card.shadowRoot.querySelectorAll(".house-row");
    assert.strictEqual(rows.length, 1);
    on.card.remove();
    const lov = await mount([EVERYONE, ...PHONES], [1280, 800], {}, { fullpage: false });
    assert.ok(!q(lov.card, "#everyone-btn"), "a Lovelace card (not the kiosk page) never draws it");
    assert.ok(!q(lov.card, "#back-rail"));
    lov.card.remove();
    const odd = await mount([{ ...EVERYONE, kind: "phone" }, { ...EVERYONE, id: "x" }, { id: "everyone", kind: "group" }], [1280, 800]);
    assert.ok(!q(odd.card, "#everyone-btn"), "only a group entry named everyone with an action id counts");
    odd.card.remove();
    console.log("PASS: the key appears only for a group `everyone` entry on a fullpage card; the back rail is untouched");
  }

  // 3. the key's states and words
  {
    const t = P.houseKeyText;
    assert.deepStrictEqual(t(EVERYONE, false), { label: "Everyone (2 phones)", sub: "", disabled: false });
    assert.strictEqual(t({ ...EVERYONE, count: 1 }, false).label, "Everyone (1 phone)");
    assert.strictEqual(t({ ...EVERYONE, count: 0 }, false).label, "Everyone", "no count is never '0 phones'");
    const none = t({ ...EVERYONE, available: false, reason: "no_receivers", count: 0 }, false);
    assert.deepStrictEqual(none, { label: "Everyone", sub: "No phone is set to receive house calls", disabled: true });
    assert.deepStrictEqual(t({ ...EVERYONE, available: false, reason: "busy" }, false).sub, "You are on a call");
    assert.deepStrictEqual(t(EVERYONE, true), { label: "Everyone", sub: "You are on a call", disabled: true });
    assert.strictEqual(t({ ...EVERYONE, available: false, reason: "zzz" }, false).sub, "Not available right now");
    const off = await mount([{ ...EVERYONE, available: false, reason: "no_receivers", count: 0 }, ...PHONES], [1280, 800]);
    const b = q(off.card, "#everyone-btn");
    assert.ok(b.disabled && b.textContent.includes("No phone is set to receive house calls"));
    b.click();
    await sleep(20);
    assert.strictEqual(off.fake.ofType("portico/content/run").length, 0, "a disabled key sends nothing");
    assert.ok(!overlay());
    off.card.remove();
    console.log("PASS: key words: ready (count), none receive, busy, unknown reason; a disabled key sends nothing");
  }

  // 4. pressing the key: no confirm step (CB-9), content/run with the roster action id and the screen's scope
  {
    const { card, fake } = await mount([EVERYONE, ...PHONES], [1280, 800], { runResult: () => ({ call_id: "hso-1", media: null }) });
    q(card, "#everyone-btn").click();
    await sleep(20);
    const runs = fake.ofType("portico/content/run");
    assert.deepStrictEqual(runs, [{ type: "portico/content/run", action_id: "r-every", endpoint: "ep-kitchen" }], "one press, one run");
    assert.ok(!q(card, ".armed") && !card.shadowRoot.textContent.includes("Tap again"), "no hold or second tap");
    assert.ok(overlay(), "the overlay is up at once");
    assert.ok(otext().includes("Calling everyone"), otext());
    assert.ok(oq("#house-cancel-btn") && !oq("#house-cancel-btn").disabled, "Cancel works once the call id is known");
    // a second press while the call is live does nothing
    q(card, "#everyone-btn").click();
    await sleep(10);
    assert.strictEqual(fake.ofType("portico/content/run").length, 1);
    console.log("PASS: Everyone: one press = one content/run {action_id r-..., endpoint}; no confirm; a second press while live is ignored");
    card.remove();
  }

  // 5. the outgoing flow: ringing N phones, who answered, in call, hang up
  {
    const { card, fake } = await mount([EVERYONE, ...PHONES], [1280, 800], { runResult: () => ({ call_id: "hso-1", media: null }) });
    q(card, "#everyone-btn").click();
    await sleep(20);
    fake.fire(hc("ringing"));
    assert.ok(otext().includes("Calling everyone") && otext().includes("Ringing 2 phones"), otext());
    assert.ok(q(card, "#everyone-btn").disabled && q(card, "#everyone-btn").textContent.includes("You are on a call"), "the key is off while live");
    fake.fire(hc("answered", { to: "ep-dina" }));
    assert.ok(otext().includes("Dina answered") && otext().includes("Connecting"), otext());
    fake.fire(hc("active", { to: "ep-dina" }));
    assert.ok(otext().includes("In a call with Dina"), otext());
    oq("#house-hangup-btn").click();
    await sleep(20);
    assert.deepStrictEqual(fake.ofType("portico/house/hangup"), [{ type: "portico/house/hangup", call_id: "hso-1" }]);
    assert.ok(!overlay(), "hanging up lets go of the screen at once");
    fake.fire(hc("ended", { reason: "hangup" }));
    assert.ok(!overlay(), "the server's end of a call we no longer show says nothing");
    assert.ok(!q(card, "#everyone-btn").disabled, "the key is ready again");
    console.log("PASS: outgoing: Calling everyone, Ringing 2 phones, <name> answered, In a call, Hang up sends house/hangup with the call id");
    card.remove();
  }

  // 6. cancel while ringing: house/hangup, silent
  {
    const { card, fake } = await mount([EVERYONE, ...PHONES], [1280, 800], { runResult: () => ({ call_id: "hso-1", media: null }) });
    q(card, "#everyone-btn").click();
    await sleep(20);
    fake.fire(hc("ringing"));
    oq("#house-cancel-btn").click();
    await sleep(20);
    assert.deepStrictEqual(fake.ofType("portico/house/hangup"), [{ type: "portico/house/hangup", call_id: "hso-1" }]);
    assert.ok(!overlay());
    fake.fire(hc("ended", { reason: "cancelled" }));
    assert.ok(!overlay(), "no outcome for our own cancel");
    card.remove();
    // Cancel is off until the call id is known (the reply has not come)
    const slow = await mount([EVERYONE, ...PHONES], [1280, 800], { runResult: () => ({}) });
    q(slow.card, "#everyone-btn").click();
    await sleep(20);
    assert.ok(oq("#house-cancel-btn").disabled, "no call id yet: Cancel waits");
    slow.fake.fire(hc("ringing"));
    assert.ok(!oq("#house-cancel-btn").disabled, "the ring event carries the id");
    slow.card.remove();
    console.log("PASS: Cancel sends house/hangup and says nothing after; it waits for the call id");
  }

  // 7. outcomes with the design's words
  {
    const cases = [
      ["declined", "Everyone declined"], ["no_answer", "No one answered"], ["busy", "Everyone is busy"],
      ["error", "Could not connect"], ["hangup", "The call ended"],
    ];
    for (const [reason, text] of cases) {
      const { card, fake } = await mount([EVERYONE, ...PHONES], [1280, 800], { runResult: () => ({ call_id: "hso-1" }) }, { _houseEndMs: 80 });
      q(card, "#everyone-btn").click();
      await sleep(20);
      fake.fire(hc("ringing"));
      fake.fire(hc("ended", { reason }));
      assert.ok(otext().includes(text), `${reason}: ${otext()}`);
      assert.ok(oq("#house-close-btn"), "an outcome can be closed");
      await sleep(120);
      assert.ok(!overlay(), `${reason}: the outcome goes by itself`);
      assert.ok(!q(card, "#everyone-btn").disabled);
      card.remove();
    }
    const { card, fake } = await mount([EVERYONE, ...PHONES], [1280, 800], { runResult: () => ({ call_id: "hso-1" }) });
    q(card, "#everyone-btn").click();
    await sleep(20);
    fake.fire(hc("ended", { reason: "declined" }));
    oq("#house-close-btn").click();
    assert.ok(!overlay(), "Close dismisses it");
    card.remove();
    console.log("PASS: outcomes: declined, no answer, busy, error, hangup each in words; they leave by themselves or on Close");
  }

  // 8. a refused placement shows the words for its code (never "everyone declined")
  {
    const codes = {
      busy: "You are already on a call.", not_callable: "No phone is set to receive house calls right now.",
      forbidden: "This screen may not call everyone.", place_forbidden: "A house call is placed from the screen itself.",
      not_available: "House calls are not switched on.", rate_limited: "Wait a moment, then try again.", boom: "The call could not be placed.",
    };
    for (const [code, text] of Object.entries(codes)) {
      const { card } = await mount([EVERYONE, ...PHONES], [1280, 800], { runError: () => Object.assign(new Error("x"), { code }) }, { _houseEndMs: 5000 });
      q(card, "#everyone-btn").click();
      await sleep(30);
      assert.ok(otext().includes(text), `${code}: ${otext()}`);
      assert.ok(!otext().includes("declined"), "a refusal is never reported as a decline");
      card.remove();
    }
    console.log("PASS: a refused placement (busy, not_callable, forbidden, place_forbidden, not_available, rate_limited, other) shows its own words");
  }

  // 9. an incoming call from a phone: ring, Answer / Decline, silent ends
  {
    const { card, fake } = await mount([EVERYONE, ...PHONES], [1280, 800]);
    fake.fire(incoming("ringing"));
    assert.ok(otext().includes("Alice is calling"), otext());
    assert.ok(oq("#house-answer-btn") && oq("#house-decline-btn"));
    assert.strictEqual(card._houseChime, true, "the chime starts");
    oq("#house-answer-btn").click();
    await sleep(20);
    assert.deepStrictEqual(fake.ofType("portico/house/answer"), [{ type: "portico/house/answer", call_id: "hso-9", client_kind: "browser" }]);
    assert.ok(otext().includes("Connecting"), otext());
    assert.strictEqual(card._houseChime, false, "the chime stops once answered");
    fake.fire(incoming("active", { from: "ep-alice" }));
    assert.ok(otext().includes("In a call with Alice") && oq("#house-hangup-btn"));
    fake.fire(incoming("ended", { reason: "hangup" }));
    assert.ok(otext().includes("The call ended"));
    card.remove();

    const d = await mount([EVERYONE, ...PHONES], [1280, 800]);
    d.fake.fire(incoming("ringing"));
    oq("#house-decline-btn").click();
    await sleep(20);
    assert.deepStrictEqual(d.fake.ofType("portico/house/decline"), [{ type: "portico/house/decline", call_id: "hso-9" }]);
    assert.ok(!overlay());
    d.fake.fire(incoming("ended", { reason: "declined" }));
    assert.ok(!overlay(), "our own decline is silent");
    d.card.remove();

    for (const reason of ["answered_elsewhere", "cancelled", "no_answer"]) {
      const e = await mount([EVERYONE, ...PHONES], [1280, 800]);
      e.fake.fire(incoming("ringing"));
      e.fake.fire(incoming("ended", { reason }));
      assert.ok(!overlay(), `${reason}: the ring just stops`);
      assert.strictEqual(e.card._houseChime, false);
      e.card.remove();
    }
    const f = await mount([EVERYONE, ...PHONES], [1280, 800], { houseError: () => Object.assign(new Error("x"), { code: "already_answered" }) });
    f.fake.fire(incoming("ringing"));
    oq("#house-answer-btn").click();
    await sleep(20);
    assert.ok(!overlay(), "a lost race lets go");
    assert.ok(f.card.shadowRoot.textContent.includes("Someone else already answered"), "and says so");
    f.card.remove();
    console.log("PASS: incoming: ring, Answer (house/answer browser), Decline, Hang up; answered-elsewhere/cancelled/no-answer are silent; a lost race says so");
  }

  // 10. the door outranks the house: the house overlay yields to a door ring and comes back after
  {
    const fake = makeHass({ doors: [DOOR_AUDIO], content: kitchen([EVERYONE, ...PHONES]), runResult: () => ({ call_id: "hso-1" }) });
    const card = doc.createElement("portico-card");
    doc.body.appendChild(card);
    card.setConfig({ _pollMs: 20, endpoint: "ep-kitchen", fullpage: true, takeover: true });
    card._box = { w: 1280, h: 800 };
    card.hass = fake.hass;
    await sleep(60);
    fake.fire(incoming("ringing"));
    assert.ok(overlay(), "the house ring shows");
    fake.fire({ type: "call_state_changed", door_id: DOOR_AUDIO.door_id, call_id: "c-1", state: "ringing", prev_state: "idle" });
    assert.ok(doc.querySelector("[data-portico-overlay]"), "the door's own takeover shows");
    assert.ok(!overlay(), "and the house overlay is out of the way");
    fake.fire({ type: "call_state_changed", door_id: DOOR_AUDIO.door_id, call_id: "c-1", state: "ended", prev_state: "ringing" });
    assert.ok(!doc.querySelector("[data-portico-overlay]"));
    card.remove();
    assert.ok(!overlay() && !doc.querySelector("[data-portico-overlay]"), "removal cleans up");
    console.log("PASS: a door ring takes the screen from a house call; nothing of the door flow changed");
  }

  // 11. events that are not ours, and cards that are not the kiosk page
  {
    const { card, fake } = await mount([EVERYONE, ...PHONES], [1280, 800]);
    fake.fire({ type: "house_call", call_id: "hso-5", state: "ringing", from: "ep-alice", to: "ep-dina" });
    assert.ok(!overlay(), "a call between two other endpoints is not shown");
    fake.fire({ type: "house_call", state: "ringing", from: "ep-alice", to: "ep-kitchen" });
    assert.ok(!overlay(), "no call id, no overlay");
    fake.fire(incoming("ringing"));
    fake.fire({ ...incoming("ringing"), call_id: "hso-10", from: "ep-dina" });
    assert.ok(otext().includes("Alice is calling"), "a second ring does not replace the live one");
    fake.fire({ ...incoming("ended", { reason: "cancelled" }), call_id: "hso-10" });
    assert.ok(otext().includes("Alice is calling"), "a stranger's end does not end ours");
    card.remove();
    const lov = await mount([EVERYONE, ...PHONES], [1280, 800], {}, { fullpage: false });
    lov.fake.fire(incoming("ringing"));
    assert.ok(!overlay(), "a Lovelace card never shows a house call");
    lov.card.remove();
    console.log("PASS: other endpoints' calls, id-less events, a second ring, and non-kiosk cards are ignored");
  }

  // 12. the roster re-reads when who can be rung changes, once per revision
  {
    const { card, fake } = await mount([EVERYONE, ...PHONES], [1280, 800]);
    const gets = () => fake.ofType("portico/content/get").length;
    const before = gets();
    fake.fire({ type: "house_roster_changed", rev: "aaa" });
    fake.fire({ type: "house_roster_changed", rev: "aaa" });
    await sleep(20);
    assert.strictEqual(gets(), before + 1, "one read for a new revision");
    fake.fire({ type: "house_roster_changed", rev: "bbb" });
    await sleep(20);
    assert.strictEqual(gets(), before + 2);
    card.remove();
    console.log("PASS: house_roster_changed re-reads the roster once per revision");
  }

  // 13. a ring the server never ended lets go of the screen (stale timer); the card's removal clears everything
  {
    const { card, fake } = await mount([EVERYONE, ...PHONES], [1280, 800], {}, { _houseStaleMs: 60 });
    fake.fire(incoming("ringing"));
    assert.ok(overlay());
    await sleep(120);
    assert.ok(!overlay(), "the stale ring is dropped");
    assert.strictEqual(card._houseChime, false);
    fake.fire(incoming("ringing"));
    card.remove();
    assert.ok(!overlay(), "removing the card removes its overlay");
    console.log("PASS: a ring the server never ended is dropped after the stale limit; removal clears the overlay");
  }

  // 14. the state machine, pure
  {
    const R = P.houseReduce;
    const own = "ep-kitchen";
    const e = (state, extra) => ({ type: "house_call", call_id: "c1", state, from: own, to: "everyone", ...extra });
    const placing = { callId: null, dir: "out", state: "placing", peer: null, group: true, count: 2, reason: null };
    let s = R(placing, e("ringing"), own);
    assert.strictEqual(s.callId, "c1"); assert.strictEqual(s.state, "ringing"); assert.strictEqual(s.count, 2, "the count survives");
    s = R(s, e("answered", { to: "ep-dina" }), own);
    assert.strictEqual(s.peer, "ep-dina"); assert.strictEqual(s.state, "answered");
    s = R(s, e("active", { to: "ep-dina" }), own);
    assert.strictEqual(s.state, "active");
    assert.strictEqual(R(s, e("ended", { reason: "hangup", to: "ep-dina" }), own).state, "ended");
    assert.strictEqual(R(null, e("ended", { reason: "declined" }), own), null, "an end for nothing shown is ignored");
    assert.strictEqual(R(null, e("ringing", { to: "ep-dina" }), own).group, false, "a 1:1 button call");
    assert.strictEqual(R(null, e("ringing", { to: "ep-dina" }), own).peer, "ep-dina");
    const ended = { callId: "c1", dir: "out", state: "ended", reason: "declined", group: true };
    assert.strictEqual(R(ended, e("active"), own), ended, "a finished call stays finished");
    assert.strictEqual(R(ended, e("ringing", { call_id: "c2" }), own).callId, "c2", "a new call replaces a shown outcome");
    assert.strictEqual(R(s, e("ringing", { call_id: "c2" }), own), s, "a live call is not displaced");
    assert.strictEqual(R(s, e("ended", { call_id: "c2", reason: "error" }), own), s);
    assert.strictEqual(R(placing, { type: "house_call", call_id: "z", state: "ringing", from: "ep-alice", to: own }, own), placing, "a ring from a phone while we place is not ours to show");
    assert.strictEqual(R(s, { type: "nope" }, own), s);
    assert.strictEqual(R(s, e("weird"), own), s);
    assert.strictEqual(R(null, { type: "house_call", call_id: "q", state: "ringing", from: "a", to: "b" }, own), null);
    assert.strictEqual(R(s, e("ended", { reason: "cancelled" }), own), null, "our own cancel");
    assert.strictEqual(R(s, e("ended", { reason: "answered_elsewhere" }), own), null);
    const inb = R(null, { type: "house_call", call_id: "c3", state: "ringing", from: "ep-alice", to: own }, own);
    assert.deepStrictEqual([inb.dir, inb.peer, inb.state], ["in", "ep-alice", "ringing"]);
    assert.strictEqual(R(inb, { type: "house_call", call_id: "c3", state: "ended", reason: "no_answer", from: "ep-alice", to: own }, own), null);
    const inAns = { ...inb, state: "answered" };
    assert.strictEqual(R(inAns, { type: "house_call", call_id: "c3", state: "ended", reason: "error", from: "ep-alice", to: own }, own).reason, "error");
    console.log("PASS: the overlay state machine: placing -> ringing -> answered -> active -> ended, late and foreign events, silent ends");
  }

  // 15. outcome and refusal words, pure
  {
    const o = P.houseOutcomeText;
    assert.strictEqual(o({ reason: "declined", group: false }, "Dina"), "Dina declined");
    assert.strictEqual(o({ reason: "no_answer", group: false }, "Dina"), "Dina did not answer");
    assert.strictEqual(o({ reason: "busy", group: false }, "Dina"), "Dina is on a call");
    assert.strictEqual(o({ reason: "dnd", group: false }, "Dina"), "Dina has Do Not Disturb on");
    assert.strictEqual(o({ reason: "declined", group: false }, ""), "Someone declined");
    assert.strictEqual(o({ reason: "refused", text: "T" }, ""), "T");
    assert.strictEqual(o({ reason: "refused" }, ""), "The call could not be placed.");
    assert.strictEqual(o({ reason: "wat" }, ""), "The call ended");
    assert.strictEqual(P.houseRefusalText("nonsense"), "The call could not be placed.");
    console.log("PASS: outcome words for 1:1 and group; refusal fallback");
  }

  // 16. the layout arithmetic with the Everyone row: it fits at the four tablets and costs exactly one row
  {
    const geo = (w, h, n, nb, extra = {}) => P.wideGeometry({ w, h, fullpage: true, nMain: n ? 1 : 0, nSide: Math.max(0, n - 1), nButtons: nb, hasTitle: true, extras: 0, ...extra });
    for (const [w, h] of [[1280, 800], [1920, 1200], [1024, 600], [800, 480]]) {
      for (const n of [1, 3, 5]) {
        for (const nb of [0, 3, 8]) {
          for (const extras of [0, 2]) {
            const g = geo(w, h, n, nb, { house: 1, extras });
            const tag = `${w}x${h} ${n} cams ${nb} buttons ${extras} extras`;
            assert.ok(g.height <= h + 0.001, `${tag}: total ${g.height} > ${h}`);
            assert.strictEqual(g.houseH, P.W_HOUSE);
            const base = geo(w, h, n, nb, { house: 0, extras });
            assert.strictEqual(base.houseH, 0);
            assert.ok(g.extrasShown <= base.extrasShown, `${tag}: the row never makes room for MORE later blocks`);
            if (g.extrasShown === base.extrasShown) assert.ok(g.mw <= base.mw + 0.001, `${tag}: the row never makes the main camera larger`);
            assert.ok(g.groupW <= w - P.W_RAIL - 16 + 0.001, `${tag}: group within the card beside the rail`);
            assert.ok(Math.abs(g.mh - g.mw * 9 / 16) < 0.001, `${tag}: 16:9 kept`);
          }
        }
      }
    }
    const a = geo(1280, 800, 3, 4, { house: 0 });
    const b = geo(1280, 800, 3, 4, { house: 1 });
    assert.strictEqual(geo(1280, 800, 3, 4, {}).height, a.height, "house omitted = house 0");
    assert.strictEqual(P.wideGeometry({ w: 1280, h: 800, fullpage: false, nMain: 1, nSide: 2, nButtons: 3, hasTitle: true, extras: 0, house: 1 }).houseH, 0, "a Lovelace card has no row");
    assert.ok(b.mw < a.mw || b.height >= a.height, "the row is paid for out of the main camera or the free space");
    console.log("PASS: wide arithmetic with the Everyone row: fits 1280x800, 1920x1200, 1024x600, 800x480 for 1/3/5 cameras, 0/3/8 buttons, 0/2 later blocks; costs one row; Lovelace has none");
  }

  // 17. the page itself in the wide layout: the key sits under the blocks, the camera arithmetic carries the row
  {
    for (const [w, h] of [[1280, 800], [1024, 600], [800, 480]]) {
      const on = await mount([EVERYONE, ...PHONES], [w, h]);
      const off = await mount(PHONES, [w, h]);
      const mwOf = (c) => parseFloat(q(c.card, ".block.wide").style.getPropertyValue("--w-mw"));
      const g = P.wideGeometry({ w, h, fullpage: true, nMain: 1, nSide: 2, nButtons: 2, hasTitle: true, extras: 1, house: 1 });
      assert.ok(Math.abs(mwOf(on) - Math.floor(g.mw * 100) / 100) < 0.011, `${w}x${h}: the page uses the arithmetic with the row`);
      assert.ok(mwOf(on) <= mwOf(off) + 0.001);
      const kids = Array.from(q(on.card, ".content").children);
      assert.ok(kids[kids.length - 1].classList.contains("house-row"), "the row is last, under every block");
      assert.ok(q(on.card, "#back-rail") && q(off.card, "#back-rail"));
      assert.strictEqual(q(on.card, "#back-rail").getAttribute("style"), q(off.card, "#back-rail").getAttribute("style"), "the back rail is byte-identical");
      on.card.remove();
      off.card.remove();
    }
    console.log("PASS: wide page: the key is the last row, the main camera follows the arithmetic with the row, the back rail's style is identical");
  }

  // 18. the overlay fits each tablet
  {
    for (const [w, h] of [[1280, 800], [1920, 1200], [1024, 600], [800, 480]]) {
      const g = P.houseOverlayGeometry(w, h);
      assert.ok(g.total <= h, `${w}x${h}: ${g.total} > ${h}`);
      assert.ok(2 * g.btnW + g.gap <= w - 2 * g.pad + 0.001, `${w}x${h}: two buttons side by side`);
      assert.ok(g.btnH >= 48, "a touch target");
    }
    const { card, fake } = await mount([EVERYONE, ...PHONES], [800, 480]);
    fake.fire(incoming("ringing"));
    const css = overlay().shadowRoot.querySelector("style").textContent;
    assert.ok(css.includes(`height: ${P.houseOverlayGeometry(800, 480).btnH}px`), "the overlay uses the arithmetic");
    card.remove();
    console.log("PASS: the house overlay's title, status and buttons fit 1280x800, 1920x1200, 1024x600, 800x480");
  }

  console.log("ALL_CARD_HOUSE_TESTS_PASSED");
  process.exit(0);
}

main().catch((err) => { console.error(err); process.exit(1); });
