import crypto from "crypto";

/**
 * Decode a Base32 string (RFC 4648) into a Buffer.
 * Handles lowercase, spaces, and padding gracefully.
 */
function base32Decode(input: string): Buffer {
  const ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567";
  const clean = input.toUpperCase().replace(/\s+/g, "").replace(/=+$/, "");
  let bits = 0;
  let value = 0;
  const output: number[] = [];

  for (const char of clean) {
    const idx = ALPHABET.indexOf(char);
    if (idx === -1) continue;
    value = (value << 5) | idx;
    bits += 5;
    if (bits >= 8) {
      output.push((value >>> (bits - 8)) & 0xff);
      bits -= 8;
    }
  }

  return Buffer.from(output);
}

/**
 * Generate a TOTP code (RFC 6238) from a Base32 secret.
 * @param secret   Base32-encoded TOTP secret (from the QR code / authenticator app setup)
 * @param digits   Number of digits (default 6)
 * @param period   Time step in seconds (default 30)
 * @returns        Zero-padded OTP code string, e.g. "048271"
 */
export function generateTOTP(
  secret: string,
  digits = 6,
  period = 30,
): string {
  const key = base32Decode(secret);
  const counter = Math.floor(Date.now() / 1000 / period);

  // Counter as big-endian 8-byte buffer
  const buf = Buffer.alloc(8);
  const hi = Math.floor(counter / 0x100000000);
  const lo = counter >>> 0;
  buf.writeUInt32BE(hi, 0);
  buf.writeUInt32BE(lo, 4);

  const hmac = crypto.createHmac("sha1", key).update(buf).digest();
  const offset = hmac[hmac.length - 1] & 0x0f;
  const code =
    (((hmac[offset] & 0x7f) << 24) |
      ((hmac[offset + 1] & 0xff) << 16) |
      ((hmac[offset + 2] & 0xff) << 8) |
      (hmac[offset + 3] & 0xff)) %
    Math.pow(10, digits);

  return code.toString().padStart(digits, "0");
}

/**
 * Seconds remaining until the current TOTP period expires.
 */
export function totpSecondsRemaining(period = 30): number {
  return period - (Math.floor(Date.now() / 1000) % period);
}
