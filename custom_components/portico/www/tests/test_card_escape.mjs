// Provider-supplied strings (door id, name, error text) reach the classic ring view through innerHTML templates;
// they must come out as text, never as markup (review finding HA-L5). The idle view is the content view, which
// builds with textContent (test_card_screen.mjs pins labels and names there).
import assert from "assert";
import { loadCard, sleep, makeHass } from "./card_loader.mjs";

const { dom, PorticoCard } = loadCard();
assert.ok(PorticoCard, "portico-card was not registered");
const doc = dom.window.document;

const HOSTILE_ID = `x"><img src=x onerror=window.__pwn=1><b>&amp;`;
const HOSTILE_NAME = `<script>window.__pwn=1</script>drv`;

async function mount(doors) {
  const fake = makeHass({ doors });
  const card = doc.createElement("portico-card");
  doc.body.appendChild(card);
  card.setConfig({});
  card.hass = fake.hass;
  await sleep(40);
  return { card, fake };
}

async function main() {
  // ringing heading (the door's name) + error line
  {
    const { card, fake } = await mount([{ door_id: HOSTILE_ID, endpoint_id: "e:1", name: HOSTILE_NAME, driver: "d", capabilities: {} }]);
    fake.fire({ type: "call_state_changed", door_id: HOSTILE_ID, call_id: "c1", state: "ringing" });
    await sleep(10);
    card._error = `<img src=x onerror=window.__pwn=1>`;
    card._render();
    const root = card.shadowRoot;
    assert.strictEqual(root.querySelector(".ringing h2").textContent, `${HOSTILE_NAME} is calling`);
    assert.strictEqual(root.querySelector(".error").textContent, `<img src=x onerror=window.__pwn=1>`);
    assert.strictEqual(root.querySelectorAll("img, b, script").length, 0, "no injected elements");
    assert.ok(!dom.window.__pwn, "nothing executed");
    console.log("PASS: a hostile door name in the ring heading and an error string render as text");
    card.remove();
  }
  // a door with no name falls back to its id, escaped
  {
    const { card, fake } = await mount([{ door_id: HOSTILE_ID, endpoint_id: "e:1", driver: "d", capabilities: {} }]);
    fake.fire({ type: "call_state_changed", door_id: HOSTILE_ID, call_id: "c1", state: "ringing" });
    await sleep(10);
    assert.strictEqual(card.shadowRoot.querySelector(".ringing h2").textContent, `${HOSTILE_ID} is calling`);
    assert.strictEqual(card.shadowRoot.querySelectorAll("img, b").length, 0, "no injected elements");
    console.log("PASS: a hostile door_id used as the heading renders as text");
    card.remove();
  }
  // safe values: markup unchanged
  {
    const { card, fake } = await mount([{ door_id: "10.3.0.20", endpoint_id: "e:1", name: "Front door", driver: "dahua_sip", capabilities: {} }]);
    fake.fire({ type: "call_state_changed", door_id: "10.3.0.20", call_id: "c1", state: "ringing" });
    await sleep(10);
    assert.strictEqual(card.shadowRoot.querySelector(".ringing h2").textContent, "Front door is calling");
    console.log("PASS: ordinary values are unchanged");
    card.remove();
  }
}
main().then(() => process.exit(0), (e) => { console.error(e); process.exit(1); });
