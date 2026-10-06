/* 纯 JavaScript SHA-256（无外部依赖、无需安全上下文）。
 * 公开领域实现，接口：
 *   Sha256.hexFromBytes(Uint8Array) -> 小写 hex
 *   Sha256.hexFromString(str)      -> UTF-8 编码后取 hex
 */
(function (global) {
  "use strict";

  var K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1,
    0x923f82a4, 0xab1c5ed5, 0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3,
    0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174, 0xe49b69c1, 0xefbe4786,
    0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147,
    0x06ca6351, 0x14292967, 0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13,
    0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85, 0xa2bfe8a1, 0xa81a664b,
    0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a,
    0x5b9cca4f, 0x682e6ff3, 0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208,
    0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);

  function rotr(x, n) {
    return (x >>> n) | (x << (32 - n));
  }

  function hashBytes(bytes) {
    var h = new Uint32Array([
      0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c,
      0x1f83d9ab, 0x5be0cd19,
    ]);

    var bitLen = bytes.length * 8;
    var paddedLen = Math.ceil((bytes.length + 9) / 64) * 64;
    var buf = new Uint8Array(paddedLen);
    buf.set(bytes);
    buf[bytes.length] = 0x80;
    // 64 位大端长度（JS 中字节长度安全，逐字节写入）
    var hi = Math.floor(bitLen / 4294967296);
    var lo = bitLen >>> 0;
    buf[paddedLen - 8] = hi >>> 24;
    buf[paddedLen - 7] = (hi >>> 16) & 0xff;
    buf[paddedLen - 6] = (hi >>> 8) & 0xff;
    buf[paddedLen - 5] = hi & 0xff;
    buf[paddedLen - 4] = lo >>> 24;
    buf[paddedLen - 3] = (lo >>> 16) & 0xff;
    buf[paddedLen - 2] = (lo >>> 8) & 0xff;
    buf[paddedLen - 1] = lo & 0xff;

    var w = new Uint32Array(64);

    for (var chunk = 0; chunk < paddedLen; chunk += 64) {
      for (var t = 0; t < 16; t++) {
        var o = chunk + t * 4;
        w[t] = (buf[o] << 24) | (buf[o + 1] << 16) | (buf[o + 2] << 8) | buf[o + 3];
      }
      for (t = 16; t < 64; t++) {
        var v15 = w[t - 15];
        var v2 = w[t - 2];
        var s0 = rotr(v15, 7) ^ rotr(v15, 18) ^ (v15 >>> 3);
        var s1 = rotr(v2, 17) ^ rotr(v2, 19) ^ (v2 >>> 10);
        w[t] = (w[t - 16] + s0 + w[t - 7] + s1) | 0;
      }

      var a = h[0], b = h[1], c = h[2], d = h[3];
      var e = h[4], f = h[5], g = h[6], hh = h[7];

      for (t = 0; t < 64; t++) {
        var S1 = rotr(e, 6) ^ rotr(e, 11) ^ rotr(e, 25);
        var ch = (e & f) ^ (~e & g);
        var t1 = (hh + S1 + ch + K[t] + w[t]) | 0;
        var S0 = rotr(a, 2) ^ rotr(a, 13) ^ rotr(a, 22);
        var maj = (a & b) ^ (a & c) ^ (b & c);
        var t2 = (S0 + maj) | 0;
        hh = g; g = f; f = e; e = (d + t1) | 0;
        d = c; c = b; b = a; a = (t1 + t2) | 0;
      }

      h[0] = (h[0] + a) | 0; h[1] = (h[1] + b) | 0;
      h[2] = (h[2] + c) | 0; h[3] = (h[3] + d) | 0;
      h[4] = (h[4] + e) | 0; h[5] = (h[5] + f) | 0;
      h[6] = (h[6] + g) | 0; h[7] = (h[7] + hh) | 0;
    }

    var out = new Uint8Array(32);
    for (var i = 0; i < 8; i++) {
      out[i * 4] = (h[i] >>> 24) & 0xff;
      out[i * 4 + 1] = (h[i] >>> 16) & 0xff;
      out[i * 4 + 2] = (h[i] >>> 8) & 0xff;
      out[i * 4 + 3] = h[i] & 0xff;
    }
    return out;
  }

  function toHex(bytes) {
    var s = "";
    for (var i = 0; i < bytes.length; i++) {
      s += bytes[i].toString(16).padStart(2, "0");
    }
    return s;
  }

  function utf8Bytes(str) {
    return new TextEncoder().encode(str);
  }

  global.Sha256 = {
    hashBytes: hashBytes,
    hexFromBytes: function (b) { return toHex(hashBytes(b)); },
    hexFromString: function (s) { return toHex(hashBytes(utf8Bytes(s))); },
    utf8Bytes: utf8Bytes,
  };
})(window);
