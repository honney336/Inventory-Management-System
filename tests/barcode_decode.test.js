/* Decode a rendered barcode back to its value, the way a scanner would.
 *
 * The renderer and this decoder were written from opposite directions: the
 * encoder walks the EAN and Code128 tables forwards, the decoder measures the
 * scan line it is handed and reconstructs the value. A wrong table, a missing
 * quiet zone or a bar drawn at the wrong width makes the decode fail.
 *
 * Run with: node tests/barcode_decode.test.js
 */
const assert = require("assert");
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const source = fs.readFileSync(
  path.join(__dirname, "..", "inventory", "static", "js", "barcode.js"),
  "utf8"
);
const C128 = JSON.parse(/var CODE128 = (\[[\s\S]*?\]);/.exec(source)[1]);
const C128_TABLE = new Map(C128.map((pattern, index) => [pattern, index]));

const sandbox = { window: {}, devicePixelRatio: 1 };
vm.createContext(sandbox);
vm.runInContext(source, sandbox);
const bc = sandbox.window.InventoryBarcode;

const EAN_L = {
  "0001101": 0, "0011001": 1, "0010011": 2, "0111101": 3, "0100011": 4,
  "0110001": 5, "0101111": 6, "0111011": 7, "0110111": 8, "0001011": 9
};
const EAN_G = {
  "0100111": 0, "0110011": 1, "0011011": 2, "0100001": 3, "0011101": 4,
  "0111001": 5, "0000101": 6, "0010001": 7, "0001001": 8, "0010111": 9
};
const EAN_R = {
  "1110010": 0, "1100110": 1, "1101100": 2, "1000010": 3, "1011100": 4,
  "1001110": 5, "1010000": 6, "1000100": 7, "1001000": 8, "1110100": 9
};
const PARITY = ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
  "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"];

/* Render into a recording canvas, then read the middle scan line back. */
function renderAndMeasure(value, options, ratio) {
  const ratioUsed = ratio || 1;
  const box = { w: 1200, h: 400 };
  const pixels = new Uint8ClampedArray(box.w * box.h * 4).fill(255);
  const bars = [];

  const context = {
    fillStyle: "#ffffff",
    font: "", textAlign: "", textBaseline: "",
    setTransform() {},
    clearRect() {},
    fillRect(x, y, w, h) {
      const dark = String(this.fillStyle).toLowerCase().indexOf("#f") === -1
        && String(this.fillStyle).toLowerCase() !== "#ffffff"
        && String(this.fillStyle).toLowerCase() !== "white";
      if (dark) bars.push({ x, y, w, h });
      const shade = dark ? 0 : 255;
      for (let py = Math.max(0, y); py < Math.min(box.h, y + h); py += 1) {
        for (let px = Math.max(0, x); px < Math.min(box.w, x + w); px += 1) {
          pixels[(py * box.w + px) * 4] = shade;
        }
      }
    },
    fillText() {}
  };

  const canvas = { width: box.w, height: box.h, style: {}, getContext: () => context };
  sandbox.window.devicePixelRatio = ratioUsed;
  const built = bc.renderToCanvas(canvas, value, options);
  if (!built) return null;

  // scan across the middle of the bars, the way a laser would
  const scanY = Math.min(box.h - 1, Math.round(((options.moduleHeightMm || 22) / 2 / 25.4) * 96 * ratioUsed));
  const bits = [];
  for (let x = 0; x < box.w; x += 1) {
    bits.push(pixels[(scanY * box.w + x) * 4] < 128 ? 1 : 0);
  }
  return { built, bits, bars, ratio: ratioUsed };
}

/* Collapse the scan line into runs, then express every run in modules. */
function toModules(measured) {
  const { bits, built } = measured;
  const modulePx = built.modulePx;
  const runs = [];
  let current = bits[0];
  let length = 0;
  for (const bit of bits) {
    if (bit === current) {
      length += 1;
    } else {
      runs.push({ bit: current, px: length });
      current = bit;
      length = 1;
    }
  }
  runs.push({ bit: current, px: length });

  const line = [];
  runs.forEach((run, index) => {
    if (run.px % modulePx !== 0) {
      // trailing canvas padding after the symbol is not part of it
      if (index === runs.length - 1) return;
      throw new Error(`run of ${run.px}px is not a whole number of ${modulePx}px modules`);
    }
    const last = line[line.length - 1];
    if (last && last.bit === run.bit) {
      last.modules += run.px / modulePx;
    } else {
      line.push({ bit: run.bit, modules: run.px / modulePx });
    }
  });

  while (line.length && line[0].bit === 0) line.shift();
  while (line.length && line[line.length - 1].bit === 0) line.pop();

  // quiet zones, then the symbol itself as a plain module string
  return {
    quiet: leadingQuietModules(bits, modulePx),
    symbol: line.map((r) => String(r.bit).repeat(r.modules)).join("")
  };
}

function leadingQuietModules(bits, modulePx) {
  let count = 0;
  for (const bit of bits) {
    if (bit === 1) break;
    count += 1;
  }
  return Math.floor(count / modulePx);
}

function decodeEAN13({ symbol, quiet }) {
  if (quiet < 9) return { error: `quiet zone is ${quiet} modules, EAN-13 needs 9` };
  if (symbol.length !== 95) return { error: `symbol is ${symbol.length} modules, EAN-13 is 95` };
  if (symbol.slice(0, 3) !== "101") return { error: "start guard wrong" };
  if (symbol.slice(45, 50) !== "01010") return { error: "centre guard wrong" };
  if (symbol.slice(92, 95) !== "101") return { error: "end guard wrong" };

  const left = [];
  for (let i = 0; i < 6; i += 1) left.push(symbol.slice(3 + i * 7, 10 + i * 7));
  const right = [];
  for (let i = 0; i < 6; i += 1) right.push(symbol.slice(50 + i * 7, 57 + i * 7));

  let first = -1;
  for (let d = 0; d < 10; d += 1) {
    const parity = PARITY[d];
    const ok = left.every((cell, i) =>
      (parity[i] === "L" ? EAN_L[cell] : EAN_G[cell]) !== undefined);
    if (ok) { first = d; break; }
  }
  if (first < 0) return { error: "no first digit matches the parity pattern" };

  const digits = [String(first)];
  for (let i = 0; i < 6; i += 1) {
    const table = PARITY[first][i] === "L" ? EAN_L : EAN_G;
    digits.push(String(table[left[i]]));
  }
  for (let i = 0; i < 6; i += 1) {
    const value = EAN_R[right[i]];
    if (value === undefined) return { error: `right digit ${i} is not an R pattern` };
    digits.push(String(value));
  }
  return { value: digits.join("") };
}

function decodeCode128({ symbol, quiet }) {
  if (quiet < 10) return { error: `quiet zone is ${quiet} modules, Code128 needs 10` };
  const symbols = [];
  let cursor = 0;
  while (symbol.length - cursor > 13) {
    symbols.push(symbol.slice(cursor, cursor + 11));
    cursor += 11;
  }
  symbols.push(symbol.slice(cursor));

  if (C128_TABLE.get(symbols[0]) !== 104) {
    return { error: `start symbol is ${C128_TABLE.get(symbols[0])}, expected 104 (Code B)` };
  }
  if (C128_TABLE.get(symbols[symbols.length - 1]) !== 106) {
    return { error: "no stop symbol" };
  }

  const codes = symbols.map((pattern) => {
    const code = C128_TABLE.get(pattern);
    return code === undefined ? -1 : code;
  });
  if (codes.indexOf(-1) !== -1) return { error: "a symbol is not a Code128 pattern" };

  // the symbol before the stop carries the checksum, which proves the
  // boundaries between characters were found correctly
  const body = codes.slice(1, -2);
  const checksum = codes[codes.length - 2];
  let sum = 104;
  body.forEach((code, index) => { sum += (index + 1) * code; });
  if (sum % 103 !== checksum) {
    return { error: `checksum is ${checksum}, expected ${sum % 103}` };
  }
  return { value: body.map((code) => String.fromCharCode(code + 32)).join("") };
}

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

const EAN_CODES = ["4006381333931", "5012345678900", "5901234123457",
  "0000000000000", "9780201379624", "1234567890128"];
const SKU_CODES = ["PCB-001", "LAB-010", "50ELEC1006000", "A", "12345678", "XY-99/Z"];
const LABEL = { widthMm: 54, moduleHeightMm: 12, displayValue: false };
const SHEET = { widthMm: 80, moduleHeightMm: 22, fontSizePt: 11 };

check("every EAN-13 decodes back to its own digits", () => {
  EAN_CODES.forEach((code) => {
    const measured = renderAndMeasure(code, SHEET);
    assert.ok(measured, `render failed for ${code}`);
    const decoded = decodeEAN13(toModules(measured));
    assert.strictEqual(decoded.error, undefined, `${code}: ${decoded.error}`);
    assert.strictEqual(decoded.value, code);
  });
});

check("a 12 digit code decodes as the 13 digit EAN it stands for", () => {
  const decoded = decodeEAN13(toModules(renderAndMeasure("400638133393", SHEET)));
  assert.strictEqual(decoded.error, undefined, decoded.error);
  assert.strictEqual(decoded.value, "4006381333931");
});

check("every Code128 value decodes back to itself", () => {
  SKU_CODES.forEach((code) => {
    const decoded = decodeCode128(toModules(renderAndMeasure(code, LABEL)));
    assert.strictEqual(decoded.error, undefined, `${code}: ${decoded.error}`);
    assert.strictEqual(decoded.value, code);
  });
});

check("every EAN-13 decodes from a shelf label sized code", () => {
  EAN_CODES.forEach((code) => {
    const decoded = decodeEAN13(toModules(renderAndMeasure(code, LABEL)));
    assert.strictEqual(decoded.error, undefined, `${code}: ${decoded.error}`);
    assert.strictEqual(decoded.value, code);
  });
});

check("bars and gaps are always whole numbers of modules", () => {
  EAN_CODES.concat(SKU_CODES).forEach((code) => {
    [12, 40, 54, 80, 150].forEach((widthMm) => {
      const measured = renderAndMeasure(code, { ...LABEL, widthMm });
      const line = toModules(measured); // throws if a run is off the module grid
      assert.ok(line.symbol.length > 40, `${code} at ${widthMm}mm gave ${line.symbol.length} modules`);
    });
  });
});

check("the module width never drops below the symbology minimum", () => {
  EAN_CODES.concat(SKU_CODES).forEach((code) => {
    [12, 40, 54, 80, 150, 400].forEach((widthMm) => {
      const measured = renderAndMeasure(code, { ...LABEL, widthMm });
      assert.ok(
        measured.built.moduleMm >= bc.MIN_MODULE_MM,
        `${code} at ${widthMm}mm gave ${measured.built.moduleMm}mm`
      );
    });
  });
});

check("the quiet zone is at least eleven modules of white on both sides", () => {
  EAN_CODES.concat(SKU_CODES).forEach((code) => {
    const measured = renderAndMeasure(code, LABEL);
    const quietPx = measured.built.quiet * measured.built.modulePx;
    const first = Math.min(...measured.bars.map((r) => r.x));
    const last = Math.max(...measured.bars.map((r) => r.x + r.w));
    assert.strictEqual(first, quietPx, `${code} left quiet zone`);
    assert.ok(measured.built.quiet >= 9, `${code} quiet zone is ${measured.built.quiet}`);
    assert.strictEqual(
      last, (measured.built.quiet + measured.built.modules.length) * measured.built.modulePx,
      `${code} right quiet zone`
    );
  });
});

check("the same value decodes at 1x, 2x and 3x pixel density", () => {
  [1, 2, 3].forEach((ratio) => {
    const ean = decodeEAN13(toModules(renderAndMeasure("4006381333931", SHEET, ratio)));
    assert.strictEqual(ean.error, undefined, `EAN at ${ratio}x: ${ean.error}`);
    assert.strictEqual(ean.value, "4006381333931");
    const sku = decodeCode128(toModules(renderAndMeasure("PCB-001", LABEL, ratio)));
    assert.strictEqual(sku.error, undefined, `Code128 at ${ratio}x: ${sku.error}`);
    assert.strictEqual(sku.value, "PCB-001");
  });
});

check("the printed width is the requested millimetres", () => {
  const measured = renderAndMeasure("4006381333931", { ...SHEET, widthMm: 80 });
  const totalModules = measured.built.modules.length + measured.built.quiet * 2;
  assert.strictEqual(
    measured.built.widthMm,
    Number((totalModules * measured.built.moduleMm).toFixed(2))
  );
  assert.ok(measured.built.widthMm >= 80, "grew to hold the minimum module width");
});

process.stdout.write(
  `\n${failures === 0 ? "barcode decoding: all checks passed" : `${failures} failure(s)`}\n`
);
process.exit(failures === 0 ? 0 : 1);