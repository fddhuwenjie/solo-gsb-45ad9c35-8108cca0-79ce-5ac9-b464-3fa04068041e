/* 浏览器端的公开规则实现（与 app/rules.py、verify/verify.py 完全一致）。 */
(function (global) {
  "use strict";

  var RULE_ID = "sha256-argmax-v1";
  var COMMIT_PREFIX = "COMMIT:";

  function isHex64(s) {
    return typeof s === "string" && /^[0-9a-f]{64}$/.test(s);
  }

  // UTF-8 字节序比较（与 Python 的 key=lambda e: e.encode('utf-8') 一致）
  function compareUtf8(a, b) {
    var ba = Sha256.utf8Bytes(a);
    var bb = Sha256.utf8Bytes(b);
    var n = Math.min(ba.length, bb.length);
    for (var i = 0; i < n; i++) {
      if (ba[i] !== bb[i]) return ba[i] - bb[i];
    }
    return ba.length - bb.length;
  }

  function canonicalSorted(entries) {
    return entries.slice().sort(compareUtf8);
  }

  function canonicalSnapshot(entries) {
    return canonicalSorted(entries).join("\n");
  }

  function listDigest(entries) {
    return Sha256.hexFromString(canonicalSnapshot(entries));
  }

  function commitForSeed(seedHex) {
    return Sha256.hexFromString(COMMIT_PREFIX + seedHex.trim().toLowerCase());
  }

  function drawWinners(entries, seedHex, prizeCount) {
    if (!(prizeCount > 0) || prizeCount > entries.length) {
      throw new Error("名额必须满足 0 < 名额 <= 人数");
    }
    var pool = canonicalSorted(entries);
    var winners = [];
    for (var i = 0; i < prizeCount; i++) {
      var bestId = null;
      var bestScore = null; // BigInt
      for (var j = 0; j < pool.length; j++) {
        var msg = RULE_ID + "|" + seedHex + "|" + pool[j] + "|" + i;
        var digest = Sha256.hashBytes(Sha256.utf8Bytes(msg));
        var score = 0n;
        for (var k = 0; k < 32; k++) {
          score = (score << 8n) | BigInt(digest[k]);
        }
        // 严格大于：平局保留字节序靠前者
        if (bestScore === null || score > bestScore) {
          bestScore = score;
          bestId = pool[j];
        }
      }
      winners.push(bestId);
      pool.splice(pool.indexOf(bestId), 1);
    }
    return winners;
  }

  function generateSeed() {
    var bytes = new Uint8Array(32);
    if (global.crypto && global.crypto.getRandomValues) {
      global.crypto.getRandomValues(bytes);
    } else {
      // 极端兜底（file:// 极旧环境）；正常现场环境不会走到
      for (var i = 0; i < 32; i++) bytes[i] = Math.floor(Math.random() * 256);
    }
    var hex = "";
    for (var j = 0; j < 32; j++) hex += bytes[j].toString(16).padStart(2, "0");
    return hex;
  }

  global.LotteryRules = {
    RULE_ID: RULE_ID,
    isHex64: isHex64,
    compareUtf8: compareUtf8,
    canonicalSorted: canonicalSorted,
    canonicalSnapshot: canonicalSnapshot,
    listDigest: listDigest,
    commitForSeed: commitForSeed,
    drawWinners: drawWinners,
    generateSeed: generateSeed,
  };
})(window);
