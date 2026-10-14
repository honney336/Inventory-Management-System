/* Tests for the barcode encoder. Run with: node tests/barcode.test.js */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "inventory", "static", "js", "barcode.js"),
  "utf8"
);

const sandbox = { window: {}, devicePixelRatio: 1 };
sandbox.window.devicePixelRatio = 1;
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const bc = sandbox.window.InventoryBarcode;

let failures = 0;
function check(name, fn) {
  try {
    fn();
    process.stdout.write(`ok   ${name}\n`);
  } catch (error) {
    failures += 1;
    process.stdout.write(`FAIL ${name}\n     ${error.message}\n`);
  }
}

const EAN = {
  "4006381333931":
    "10100011010100111010111101111010001001011001101010100001010000101000010111010010000101100110101",
  "5012345678900":
    "10100011010110011001101101111010100011011100101010101000010001001001000111010011100101110010101"
};

const C128 = {
  "PCB-001":
    "1101001000011101110110100010001101000101100010011011100100111011001001110110010011100110110111011101100011101011",
  "LAB-010":
    "1101001000010001101100101000110001000101100010011011100100111011001001110011010011101100100011011001100011101011"
};

check("every EAN-13 module string matches the reference encoding", () => {
  for (const [code, expected] of Object.entries(EAN)) {
    assert.strictEqual(bc.buildModules(code).modules, expected, `EAN ${code}`);
  }
});

check("every Code128 module string matches the reference encoding", () => {
  for (const [code, expected] of Object.entries(C128)) {
    assert.strictEqual(bc.buildModules(code).modules, expected, `Code128 ${code}`);
  }
});

check("EAN check digits are computed correctly", () => {
  assert.strictEqual(bc.ean13CheckDigit("400638133393"), 1);
  assert.strictEqual(bc.ean13CheckDigit("501234567890"), 0);
  assert.strictEqual(bc.ean13CheckDigit("000000000000"), 0);
});

check("a 12 digit code is padded and shown with its check digit", () => {
  const built = bc.buildModules("400638133393");
  assert.strictEqual(built.format, "EAN13");
  assert.strictEqual(built.text, "4006381333931");
  assert.strictEqual(built.modules, EAN["4006381333931"]);
});

check("format selection follows the value", () => {
  assert.strictEqual(bc.chooseFormat("5012345678900"), "EAN13");
  assert.strictEqual(bc.chooseFormat("501234567890"), "EAN13");
  assert.strictEqual(bc.chooseFormat("PCB-001"), "CODE128");
  assert.strictEqual(bc.chooseFormat("50ELEC1006000"), "CODE128");
  assert.strictEqual(bc.chooseFormat("12"), "CODE128");
});

check("module counts match the symbology", () => {
  assert.strictEqual(bc.buildModules("4006381333931").modules.length, 95);
  // start + characters + checksum at 11 modules each, then a 13 module stop
  ["PCB-001", "LAB-010", "50ELEC1006000"].forEach((value) => {
    const expected = 11 * (value.length + 2) + 13;
    assert.strictEqual(bc.buildModules(value).modules.length, expected, value);
  });
});

check("guard and separator patterns are in the right places", () => {
  const ean = bc.buildModules("4006381333931").modules;
  assert.ok(ean.startsWith("101"), "start guard");
  assert.ok(ean.endsWith("101"), "end guard");
  assert.strictEqual(ean.slice(45, 50), "01010", "centre separator");
});

check("unsupported characters are refused rather than drawn wrong", () => {
  assert.strictEqual(bc.buildModules("PCB-001☃"), null);
});

check("empty input is refused", () => {
  assert.strictEqual(bc.buildModules(""), null);
});

check("rendering paints one bar per dark module, all the same size", () => {
  const calls = [];
  const canvas = {
    width: 0, height: 0, style: {},
    getContext() {
      return {
        setTransform() {}, clearRect() {}, fillRect(x, y, w, h) { calls.push([x, y, w, h]); },
        fillText() {},
        set fillStyle(value) {}, get fillStyle() { return "#000"; },
        set font(value) {}, set textAlign(v) {}, set textBaseline(v) {}
      };
    }
  };
  const built = bc.renderToCanvas(canvas, "4006381333931", {
    widthMm: 80, moduleHeightMm: 22, fontSizePt: 11
  });
  assert.ok(built, "render reported success");
  assert.ok(canvas.width > 0 && canvas.height > 0, "canvas sized");

  // the first call fills the white background, the rest are the bars
  const background = calls[0];
  const bars = calls.slice(1);
  const dark = (built.modules.match(/1/g) || []).length;
  assert.strictEqual(bars.length, dark, "one fillRect per dark module");
  assert.strictEqual(new Set(bars.map((c) => c[2])).size, 1, "every bar is one module wide");
  assert.strictEqual(new Set(bars.map((c) => c[3])).size, 1, "bars share one height");
  assert.ok(bars.every((c) => c[1] === 0), "bars start at the top");
  assert.strictEqual(background[2], canvas.width, "background covers the canvas");
  assert.strictEqual(background[3], canvas.height, "background covers the canvas");
});

check("the module width is a whole number of device pixels", () => {
  const canvas = { width: 0, height: 0, style: {}, getContext: () => ({
    setTransform() {}, clearRect() {}, fillRect() {}, fillText() {},
    set fillStyle(v) {}, set font(v) {}, set textAlign(v) {}, set textBaseline(v) {}
  }) };
  const built = bc.renderToCanvas(canvas, "PCB-001", { widthMm: 54, displayValue: false });
  assert.ok(Number.isInteger(built.modulePx), `modulePx is ${built.modulePx}`);
  assert.ok(built.modulePx >= 3, `modulePx is ${built.modulePx}`);
  assert.strictEqual(
    canvas.width, (built.modules.length + built.quiet * 2) * built.modulePx
  );
});

check("the printed size never falls below the minimum module width", () => {
  const canvas = { width: 0, height: 0, style: {}, getContext: () => ({
    setTransform() {}, clearRect() {}, fillRect() {}, fillText() {},
    set fillStyle(v) {}, set font(v) {}, set textAlign(v) {}, set textBaseline(v) {}
  }) };
  [5, 20, 54, 300].forEach((widthMm) => {
    const built = bc.renderToCanvas(canvas, "4006381333931", { widthMm });
    assert.ok(
      built.moduleMm >= bc.MIN_MODULE_MM,
      `${widthMm}mm produced a ${built.moduleMm}mm module`
    );
    assert.ok(built.widthMm >= widthMm, `${widthMm}mm was not honoured`);
  });
});

check("rendering reports failure instead of throwing", () => {
  assert.strictEqual(bc.renderToCanvas({ getContext() { return null; } }, "PCB-001", {}), null);
  assert.strictEqual(bc.renderToCanvas({ getContext() { return {}; } }, "", {}), null);
});

process.stdout.write(`\n${failures === 0 ? "barcode encoder: all checks passed" : `${failures} failure(s)`}\n`);
process.exit(failures === 0 ? 0 : 1);