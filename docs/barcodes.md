# Barcodes and scanning

## What is supported

| The stored value | Encoded as | Reader returns |
|---|---|---|
| 12 or 13 digits | EAN-13 | the full 13 digits, check digit included |
| Anything else | Code128-B | the value exactly as stored |

A twelve digit code is a valid EAN-13 body. The check digit is not stored anywhere in the
product record, so the label computes it, encodes the full thirteen digits and prints all
thirteen. A scanner therefore hands back a code one digit longer than what you typed into the
product form, and the lookup accepts both.

## Drawing

`static/js/barcode.js` renders to a canvas. It has no dependencies and makes no network
requests, so labels print on a machine with no internet connection.

Two details decide whether a label scans at all:

- **Size is in millimetres, not pixels.** A label is the same physical size on screen and on
  paper, and the module width never drops below the 0.33mm minimum EAN-13 readers expect.
- **One module is a whole number of device pixels.** Drawing at a rounded pixel pitch makes
  bars up to 18% too wide or too narrow, and readers measure bar widths. Keeping every bar and
  gap on the module grid keeps their true proportions.

Quiet zones are 9 modules either side of an EAN-13 and 12 for Code128, and the renderer fills
a white background behind the bars. A value it cannot encode, such as one with non-Latin
characters, shows the code as text instead of drawing a barcode that would never read.

## Printing

- `/products/labels` prints a sheet of shelf labels, three across, one copy or several.
- The product page has a single label with a Print label button.
- EAN-13 labels print the digits under the bars; Code128 labels do not, because the SKU is
  already on the label.

Print at 100% scale with background graphics enabled. Browser print scaling below 100% shrinks
the module width under the scannable floor no matter what the app produced.

## Scanning

The scan desk at `/scan` resolves a code server side. Three ways in:

| Input | How it works |
|---|---|
| Handheld scanner | Types the code and presses Enter, then submits |
| Camera | html5-qrcode, decoded in the browser. Needs internet access for the library |
| Manual | Type into the box and submit |

The camera button is disabled with an explanation if the library did not load.

## What the lookup accepts

A scanned code is matched against the barcode field and the SKU. The barcode comparison is
normalised first, so all of these find the same product:

- the value as stored
- the value with spaces or dashes removed
- the 12 digit body and its full 13 digit EAN, in either direction
- the zero-padded 13 digit form and its 12 digit form

SKU matching ignores case. Saving two products with the same number in different forms is
refused, so two labels can never resolve to the same product.

## When a scan does not match

| Symptom | Cause and fix |
|---|---|
| App says no product matches | The stored value is not what the reader returned. A code with a unit suffix, a space in the middle of an alphanumeric code, or a prefix you added by hand has to match what is stored |
| Label prints blank | A value the encoder cannot draw, such as non-Latin characters. The code is shown as text instead |
| Label scans only from close up | Printed below 100% scale, so the module width fell under 0.33mm. Print at 100% with background graphics on |
| Nothing happens when scanning | The camera library did not load. Use a handheld scanner or the manual box |
| Camera will not start | html5-qrcode comes from a CDN. Self-host it, or use a handheld scanner |

## Tests

The encoder and the decoder were written from opposite directions, so a mistake in either
shows up as a failed round trip.

```bash
node tests/barcode.test.js          # module strings against a reference encoding
node tests/barcode_decode.test.js   # render, read the pixels back, compare
```

`tests/barcode_decode.test.js` renders a code, scans the middle scan line the way a laser
would, and reconstructs the value. The Code128 checksum is verified, which proves the symbol
boundaries were found correctly. It covers EAN-13 and Code128 at shelf-label and sheet sizes,
at 1x, 2x and 3x pixel density.