/* Barcode rendering with no external library.
 *
 * Supports EAN-13 for numeric codes and Code128-B for anything else, which
 * covers SKUs, internal codes and the EAN values on product records. The
 * previous version pulled JsBarcode from a CDN and asked it for the "auto"
 * format, a module that file does not include, so the canvas silently stayed
 * blank and labels printed unscannable.
 */
(function (global) {
  "use strict";

  var EAN13 = {
    quiet: 9,
    guard: "101",
    middle: "01010",
    L: ["0001101", "0011001", "0010011", "0111101", "0100011",
        "0110001", "0101111", "0111011", "0110111", "0001011"],
    G: ["0100111", "0110011", "0011011", "0100001", "0011101",
        "0111001", "0000101", "0010001", "0001001", "0010111"],
    R: ["1110010", "1100110", "1101100", "1000010", "1011100",
        "1001110", "1010000", "1000100", "1001000", "1110100"],
    parity: ["LLLLLL", "LLGLGG", "LLGGLG", "LLGGGL", "LGLLGG",
             "LGGLLG", "LGGGLL", "LGLGLG", "LGLGGL", "LGGLGL"]
  };

  var CODE128 = [
    "11011001100", "11001101100", "11001100110", "10010011000", "10010001100",
    "10001001100", "10011001000", "10011000100", "10001100100", "11001001000",
    "11001000100", "11000100100", "10110011100", "10011011100", "10011001110",
    "10111001100", "10011101100", "10011100110", "11001110010", "11001011100",
    "11001001110", "11011100100", "11001110100", "11101101110", "11101001100",
    "11100101100", "11100100110", "11101100100", "11100110100", "11100110010",
    "11011011000", "11011000110", "11000110110", "10100011000", "10001011000",
    "10001000110", "10110001000", "10001101000", "10001100010", "11010001000",
    "11000101000", "11000100010", "10110111000", "10110001100", "10001101100",
    "10111011000", "10111000110", "10001110110", "11101110110", "11010001110",
    "11000101110", "11011101000", "11011100010", "11011101110", "11101011000",
    "11101000110", "11100010110", "11101101000", "11101100010", "11100011010",
    "11101111010", "11001000010", "11110001010", "10100110000", "10100001100",
    "10010110000", "10010000110", "10000101100", "10000100110", "10110010000",
    "10110000100", "10011010000", "10011000010", "10000110100", "10000110010",
    "11000010010", "11001010000", "11110111010", "11000010100", "10001111010",
    "10100111100", "10010111100", "10010011110", "10111100100", "10011110100",
    "10011110010", "11110100100", "11110010100", "11110010010", "11011011110",
    "11011110110", "11110110110", "10101111000", "10100011110", "10001011110",
    "10111101000", "10111100010", "11110101000", "11110100010", "10111011110",
    "10111101110", "11101011110", "11110101110", "11010000100", "11010010000",
    "11010011100", "1100011101011"
  ];

  function isNumeric(value) {
    return /^\d+$/.test(value);
  }

  function ean13CheckDigit(first12) {
    var sum = 0;
    for (var i = 0; i < 12; i += 1) {
      var digit = parseInt(first12.charAt(i), 10);
      sum += i % 2 === 0 ? digit : digit * 3;
    }
    return (10 - (sum % 10)) % 10;
  }

  function ean13Modules(value) {
    if (value.length === 12) {
      value += ean13CheckDigit(value);
    }
    var digits = value.slice(0, 13);
    while (digits.length < 13) {
      digits = "0" + digits;
    }
    if (digits.length > 13) {
      return null;
    }
    var parity = EAN13.parity[parseInt(digits.charAt(0), 10)];
    var out = EAN13.guard;
    for (var i = 1; i <= 6; i += 1) {
      var table = parity.charAt(i - 1) === "L" ? EAN13.L : EAN13.G;
      out += table[parseInt(digits.charAt(i), 10)];
    }
    out += EAN13.middle;
    for (var j = 7; j <= 12; j += 1) {
      out += EAN13.R[parseInt(digits.charAt(j), 10)];
    }
    out += EAN13.guard;
    return out;
  }

  function code128Modules(value) {
    var codes = [104];
    for (var i = 0; i < value.length; i += 1) {
      var code = value.charCodeAt(i) - 32;
      if (code < 0 || code > 94) {
        return null;
      }
      codes.push(code);
    }
    var checksum = codes[0];
    for (var k = 1; k < codes.length; k += 1) {
      checksum += k * codes[k];
    }
    codes.push(checksum % 103);
    codes.push(106);

    var out = "";
    for (var m = 0; m < codes.length; m += 1) {
      out += CODE128[codes[m]];
    }
    return out;
  }

  function chooseFormat(value) {
    if (isNumeric(value) && (value.length === 12 || value.length === 13)) {
      return "EAN13";
    }
    return "CODE128";
  }

  function displayValue(value) {
    if (chooseFormat(value) === "EAN13" && value.length === 12) {
      return value + ean13CheckDigit(value);
    }
    return value;
  }

  function buildModules(value) {
    if (!value || !String(value).trim()) {
      return null;
    }
    var format = chooseFormat(value);
    var modules = format === "EAN13" ? ean13Modules(value) : code128Modules(value);
    if (modules === null) {
      return null;
    }
    return {
      format: format,
      modules: modules,
      text: displayValue(value),
      quiet: format === "EAN13" ? EAN13.quiet : 12
    };
  }

  /* Sizing is done in millimetres, not pixels, so a label is the same
   * physical size on screen and on paper. One module is always a whole number
   * of device pixels, which keeps every bar and gap in proportion. Drawing at a
   * rounded pixel pitch instead made bars up to 18% too wide or too narrow,
   * and put the shelf label below the 0.33mm minimum module width, which is
   * where scanners start to fail.
   */
  var MIN_MODULE_MM = 0.33;
  var CSS_DPI = 96;

  function mmToPx(mm, ratio) {
    return (mm / 25.4) * CSS_DPI * ratio;
  }

  function renderToCanvas(canvas, value, options) {
    options = options || {};
    var built = buildModules(String(value == null ? "" : value).trim());
    if (!built) {
      return null;
    }

    var context = canvas.getContext("2d");
    if (!context) {
      return null;
    }

    var ratio = window.devicePixelRatio || 1;
    var totalModules = built.modules.length + built.quiet * 2;
    var wanted = options.widthMm || 80;
    var moduleMm = Math.max(MIN_MODULE_MM, wanted / totalModules);
    var totalWidthMm = totalModules * moduleMm;
    var barHeightMm = options.moduleHeightMm || 22;
    var showText = options.displayValue !== false && built.format !== "CODE128";
    var fontPt = options.fontSizePt || 11;
    var textMm = showText ? (fontPt * 25.4) / 72 + 1.5 : 0;
    var totalHeightMm = barHeightMm + textMm;

    // whole device pixels per module keeps the bar geometry exact
    var modulePx = Math.max(3, Math.round(mmToPx(moduleMm, ratio)));
    var barPx = Math.max(4, Math.round(mmToPx(barHeightMm, ratio)));
    var fontPx = Math.round((fontPt / 72) * CSS_DPI * ratio);
    var gapPx = Math.round(mmToPx(1.5, ratio));

    canvas.width = totalModules * modulePx;
    canvas.height = Math.round(mmToPx(totalHeightMm, ratio));
    canvas.style.width = totalWidthMm.toFixed(2) + "mm";
    canvas.style.height = totalHeightMm.toFixed(2) + "mm";

    context.setTransform(1, 0, 0, 1, 0, 0);
    context.fillStyle = options.background || "#ffffff";
    context.fillRect(0, 0, canvas.width, canvas.height);
    context.fillStyle = options.dark || "#111111";

    var origin = built.quiet * modulePx;
    for (var i = 0; i < built.modules.length; i += 1) {
      if (built.modules.charAt(i) === "1") {
        context.fillRect(origin + i * modulePx, 0, modulePx, barPx);
      }
    }

    if (showText) {
      context.font = "600 " + fontPx + "px -apple-system, Segoe UI, Roboto, sans-serif";
      context.textAlign = "center";
      context.textBaseline = "top";
      context.fillText(built.text, canvas.width / 2, barPx + gapPx);
    }

    built.widthMm = Number(totalWidthMm.toFixed(2));
    built.heightMm = Number(totalHeightMm.toFixed(2));
    built.moduleMm = Number(moduleMm.toFixed(3));
    built.modulePx = modulePx;
    return built;
  }

  global.InventoryBarcode = {
    buildModules: buildModules,
    chooseFormat: chooseFormat,
    ean13CheckDigit: ean13CheckDigit,
    displayValue: displayValue,
    renderToCanvas: renderToCanvas,
    MIN_MODULE_MM: MIN_MODULE_MM
  };
}(window));