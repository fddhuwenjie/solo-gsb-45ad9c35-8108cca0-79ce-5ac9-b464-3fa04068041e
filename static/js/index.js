/* 首页：本地生成种子、只提交承诺、列出轮次。 */
(function () {
  "use strict";

  var state = { seed: null, commitment: null };

  var $ = function (id) { return document.getElementById(id); };
  var flashEl = $("flash");

  function regenerate() {
    state.seed = LotteryRules.generateSeed();
    state.commitment = LotteryRules.commitForSeed(state.seed);
    $("seedText").textContent = state.seed;
    $("commitText").textContent = state.commitment;
    $("seedArea").style.display = "block";
    $("savedConfirm").checked = false;
    $("createBtn").disabled = true;
  }

  $("genSeedBtn").addEventListener("click", regenerate);

  $("savedConfirm").addEventListener("change", function () {
    $("createBtn").disabled = !this.checked;
  });

  $("copySeedBtn").addEventListener("click", function () {
    UI.copyText(state.seed).then(function () { UI.flash(flashEl, "种子已复制", "ok"); });
  });
  $("copyCommitBtn").addEventListener("click", function () {
    UI.copyText(state.commitment).then(function () { UI.flash(flashEl, "承诺已复制", "ok"); });
  });
  $("downloadSeedBtn").addEventListener("click", function () {
    var content =
      "现场抽签秘密种子（请妥善保密保存，封存后凭它揭晓）\n" +
      "seed = " + state.seed + "\n" +
      "commitment = " + state.commitment + "\n";
    UI.downloadFile("seed.txt", content, "text/plain");
  });

  $("createBtn").addEventListener("click", async function () {
    var title = $("title").value.trim();
    var prizeCount = parseInt($("prizeCount").value, 10);
    if (!title) { UI.flash(flashEl, "请填写轮次名称", "err"); return; }
    if (!(prizeCount >= 1)) { UI.flash(flashEl, "中奖名额至少为 1", "err"); return; }

    var btn = this;
    btn.disabled = true;
    try {
      var data = await UI.api("/api/rounds", {
        method: "POST",
        body: JSON.stringify({ title: title, prizeCount: prizeCount, commitment: state.commitment }),
      });
      UI.flash(
        flashEl,
        "轮次已创建（服务器只收到承诺）。即将进入轮次页面登记参与者…",
        "ok"
      );
      setTimeout(function () { window.location.href = "/rounds/" + data.round.id; }, 900);
    } catch (e) {
      UI.flash(flashEl, e.message, "err");
      btn.disabled = false;
    }
  });

  function statusBadge(status) {
    var map = { open: "登记中", sealed: "已封存", revealed: "已揭晓" };
    return '<span class="badge badge-' + UI.esc(status) + '">' + (map[status] || status) + "</span>";
  }

  async function loadRounds() {
    try {
      var data = await UI.api("/api/rounds");
      var tbody = $("roundsBody");
      if (!data.rounds.length) {
        tbody.innerHTML = '<tr><td colspan="6" class="muted">还没有轮次，请先在上方创建。</td></tr>';
        return;
      }
      tbody.innerHTML = data.rounds.map(function (r) {
        return (
          "<tr>" +
          "<td>" + UI.esc(r.title) + "</td>" +
          '<td><a class="round-link" href="/rounds/' + encodeURIComponent(r.id) + '">' + UI.esc(r.id) + "</a></td>" +
          "<td>" + r.prizeCount + "</td>" +
          "<td>" + statusBadge(r.status) + "</td>" +
          '<td class="muted">' + UI.esc(r.createdAt || "") + "</td>" +
          '<td><a class="btn" href="/rounds/' + encodeURIComponent(r.id) + '">进入</a></td>' +
          "</tr>"
        );
      }).join("");
    } catch (e) {
      $("roundsBody").innerHTML =
        '<tr><td colspan="6" style="color:var(--err)">' + UI.esc(e.message) + "</td></tr>";
    }
  }

  loadRounds();
})();
