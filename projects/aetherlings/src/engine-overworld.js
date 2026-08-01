/* Aetherlings — the overworld: walking, encounters, NPCs, and the script runner
   that plays out every conversation and cutscene in data-story.js. */
(function (AE) {
  'use strict';

  var TS = AE.TS;
  var STEP_MS = 150;          /* time to cross one tile */
  var TURN_MS = 70;           /* facing change before a step */

  var DELTA = { n: [0, -1], s: [0, 1], e: [1, 0], w: [-1, 0] };

  function weightedPick(table) {
    var total = table.reduce(function (a, e) { return a + e.w; }, 0);
    var r = Math.random() * total;
    for (var i = 0; i < table.length; i++) {
      r -= table[i].w;
      if (r <= 0) return table[i];
    }
    return table[table.length - 1];
  }

  /* Tiles the player has permanently cleared (Cleave / Shatter), per save. */
  function clearedKey(mapId, x, y) { return mapId + ':' + x + ',' + y; }

  AE.applyCleared = function (g) {
    Object.keys(g.flags).forEach(function (k) {
      if (k.indexOf('clear:') !== 0) return;
      var parts = k.slice(6).split(':');
      var coords = parts[1].split(',');
      var m = AE.MAPS[parts[0]];
      if (m) m.grid[+coords[1]][+coords[0]] = m.indoor ? '-' : '.';
    });
  };

  /* NPCs whose visibility conditions are currently met. */
  AE.visibleNPCs = function (g, mapId) {
    var list = AE.NPCS[mapId] || [];
    return list.filter(function (n) {
      if (n.once && g.flags[n.once]) return false;
      if (n.requireFlag && !g.flags[n.requireFlag]) return false;
      if (n.requireBadges && g.badges.length < n.requireBadges) return false;
      return true;
    });
  };

  function npcDefeatFlag(mapId, npc) { return 'won:' + mapId + ':' + npc.x + ',' + npc.y; }

  AE.OverworldScene = function () {
    var g = AE.game;

    /* movement animation state */
    var moving = false, fromX = 0, fromY = 0, progress = 0, stepFrame = 0, animAcc = 0;
    var turning = 0, hopping = false;
    var surfing = false;
    var stepsSinceEncounter = 0;

    var mode = 'walk';        /* walk | script | fade | prompt */
    var fade = 0, fadeDir = 0, fadeAction = null;

    /* script runner */
    var script = null, scriptIndex = 0, scriptNPC = null;
    var dialogue = null, dialogueLines = [], dialogueTimer = 0;
    var prompt = null;        /* { text, onYes, onNo, index } */
    var resumeAfterScene = null;

    function map() { return AE.map(g.map); }

    /* ---------------- collision ---------------- */
    function npcAt(x, y) {
      var list = AE.visibleNPCs(g, g.map);
      for (var i = 0; i < list.length; i++) {
        if (list[i].sign) continue;
        if (list[i].x === x && list[i].y === y) return list[i];
      }
      return null;
    }

    function gateAt(x, y) {
      for (var i = 0; i < AE.GATES.length; i++) {
        var gate = AE.GATES[i];
        if (gate.map === g.map && gate.x === x && gate.y === y) return gate;
      }
      return null;
    }

    function walkable(x, y, dir) {
      var m = map();
      if (x < 0 || y < 0 || x >= m.w || y >= m.h) return false;
      var ch = AE.tileAt(m, x, y);

      if (ch === '~') return surfing || AE.hasSkill(g, 'surge');
      if (ch === 'C') return AE.hasSkill(g, 'ascend');
      if (ch === '^') return dir === 's';          /* ledges are one-way, southward */
      if (AE.isSolid(ch)) return false;
      if (npcAt(x, y)) return false;

      var gate = gateAt(x, y);
      if (gate && g.badges.length < gate.badges) return false;
      return true;
    }

    /* ---------------- movement ---------------- */
    function tryStep(dir) {
      if (moving || mode !== 'walk') return;

      if (g.dir !== dir) {
        g.dir = dir;
        turning = TURN_MS;
        return;
      }
      if (turning > 0) return;

      var d = DELTA[dir];
      var nx = g.x + d[0], ny = g.y + d[1];

      var gate = gateAt(nx, ny);
      if (gate && g.badges.length < gate.badges) { showDialogue([gate.text]); return; }

      if (!walkable(nx, ny, dir)) return;

      var landing = AE.tileAt(map(), nx, ny);
      hopping = landing === '^';
      if (hopping) { ny += 1; if (!walkable(nx, ny, 's')) { hopping = false; return; } }

      fromX = g.x; fromY = g.y;
      g.x = nx; g.y = ny;
      moving = true; progress = 0;
    }

    function onArrive() {
      var m = map(), ch = AE.tileAt(m, g.x, g.y);

      /* Surfing starts when you enter water and ends when you leave it. */
      if (ch === '~') surfing = true;
      else if (surfing) surfing = false;

      var warp = AE.warpAt(m, g.x, g.y);
      if (warp) { doWarp(warp); return; }

      var encTable = surfing ? m.water : (AE.isEncounterTile(ch) ? m.enc : null);
      if (encTable && encTable.table && encTable.table.length) {
        stepsSinceEncounter++;
        if (stepsSinceEncounter > 2 && AE.chance(encTable.rate)) {
          stepsSinceEncounter = 0;
          var pick = weightedPick(encTable.table);
          startWild(pick.sp, AE.randInt(pick.min, pick.max));
          return;
        }
      }
      checkTrainerSight();
    }

    /* ---------------- transitions ---------------- */
    function fadeTo(action) {
      mode = 'fade'; fade = 0; fadeDir = 1; fadeAction = action;
    }

    function doWarp(w) {
      fadeTo(function () {
        g.map = w.to;
        g.x = w.tx; g.y = w.ty;
        g.dir = w.dir || 's';
        g.visited[w.to] = true;
        surfing = AE.tileAt(AE.map(w.to), w.tx, w.ty) === '~';
        stepsSinceEncounter = 0;
      });
    }

    AE.owWarp = function (to, x, y) {
      fadeTo(function () {
        g.map = to; g.x = x; g.y = y; g.dir = 's';
        g.visited[to] = true;
        surfing = false;
      });
    };

    /* ---------------- battles ---------------- */
    function startWild(sp, lvl) {
      var foe = AE.makeMon(sp, lvl);
      fadeTo(function () {
        AE.push(AE.BattleScene({
          foe: foe,
          onEnd: function (r) {
            fade = 1; fadeDir = -1; mode = 'fade'; fadeAction = null;
            if (r === 'lose') blackout();
            else resumeScript();
          }
        }));
        fade = 0; fadeDir = 0; mode = 'walk';
      });
    }
    AE.startWild = startWild;

    function startTrainerBattle(trainer, npc) {
      fadeTo(function () {
        AE.push(AE.BattleScene({
          trainer: trainer,
          onEnd: function (r) {
            fade = 1; fadeDir = -1; mode = 'fade'; fadeAction = null;
            if (r === 'lose') { blackout(); return; }
            if (npc) g.flags[npcDefeatFlag(g.map, npc)] = true;
            resumeScript();
          }
        }));
        fade = 0; fadeDir = 0; mode = 'walk';
      });
    }

    function blackout() {
      script = null; scriptNPC = null; dialogue = null;
      AE.healParty(g);
      var townId = AE.RESPAWN[g.map] || 'willowmere';
      var t = AE.townEntry(townId);
      g.map = t.map; g.x = t.x; g.y = t.y; g.dir = 's';
      surfing = false;
      mode = 'walk';
      showDialogue([g.name + ' scrambled back to the ' + t.name + ' Hearth.',
                    'Your team was patched up and rested.']);
    }

    /* ---------------- trainer line of sight ---------------- */
    function checkTrainerSight() {
      if (mode !== 'walk') return;
      var list = AE.visibleNPCs(g, g.map);
      for (var i = 0; i < list.length; i++) {
        var n = list[i];
        if (!n.trainer || n.sign) continue;
        if (!n.sight) continue;
        if (g.flags[npcDefeatFlag(g.map, n)]) continue;

        var d = DELTA[n.dir];
        for (var step = 1; step <= n.sight; step++) {
          var sx = n.x + d[0] * step, sy = n.y + d[1] * step;
          if (AE.isSolid(AE.tileAt(map(), sx, sy))) break;
          if (sx === g.x && sy === g.y) {
            runScript((n.lines || []).concat([{ battle: n.trainer }]), n);
            return;
          }
        }
      }
    }

    /* ---------------- interaction ---------------- */
    function interact() {
      var d = DELTA[g.dir];
      var tx = g.x + d[0], ty = g.y + d[1];
      var m = map();
      var ch = AE.tileAt(m, tx, ty);

      /* NPC or sign standing there? */
      var list = AE.visibleNPCs(g, g.map);
      for (var i = 0; i < list.length; i++) {
        var n = list[i];
        if (n.x !== tx || n.y !== ty) continue;
        if (n.sign) { showDialogue(n.lines); return; }
        if (n.heal) { runScript(hearthScript(), n); return; }
        if (n.shop) { runScript([{ shop: n.shop }], n); return; }
        if (n.trainer && g.flags[npcDefeatFlag(g.map, n)]) {
          showDialogue(n.trainer.win || ['...']);
          return;
        }
        if (n.trainer) {
          runScript((n.lines || []).concat([{ battle: n.trainer }]), n);
          return;
        }
        runScript(n.lines, n);
        return;
      }

      /* Healing machine tile inside a Hearth. */
      if (ch === 'P') { runScript(hearthScript(), null); return; }

      /* Field-skill obstacles. */
      var obs = AE.OBSTACLES[ch];
      if (obs) {
        var skill = AE.FIELD_SKILLS.find(function (s) { return s.id === obs.skill; });
        if (!AE.hasSkill(g, obs.skill)) { showDialogue([obs.text]); return; }
        if (obs.skill === 'surge') {
          if (surfing) return;
          askPrompt('The water is deep. Ride across with Surge?', function () {
            g.dir = g.dir; surfing = true;
            fromX = g.x; fromY = g.y; g.x = tx; g.y = ty; moving = true; progress = 0;
          });
          return;
        }
        if (obs.skill === 'ascend') { showDialogue(['You can climb this with Ascend. Just walk into it.']); return; }
        askPrompt('Use ' + skill.name + ' to ' + skill.verb + '?', function () {
          m.grid[ty][tx] = m.indoor ? '-' : '.';
          g.flags['clear:' + clearedKey(g.map, tx, ty)] = true;
          showDialogue(['Your team used ' + skill.name + '!']);
        });
        return;
      }
    }

    function hearthScript() {
      return [
        'Attendant: Let\'s get your team rested.',
        { heal: true },
        'Attendant: There — everyone\'s back on their feet.',
        'Attendant: Come back any time.'
      ];
    }

    /* ---------------- dialogue + prompts ---------------- */
    function showDialogue(lines) {
      dialogue = lines.slice();
      dialogueTimer = 0;
      mode = 'script';
      if (!script) scriptNPC = null;
    }

    function askPrompt(text, onYes, onNo) {
      prompt = { text: text, onYes: onYes, onNo: onNo, index: 0 };
      mode = 'prompt';
    }

    /* ---------------- script runner ---------------- */
    function runScript(lines, npc) {
      if (!lines || !lines.length) return;
      script = lines.slice();
      scriptIndex = 0;
      scriptNPC = npc;
      mode = 'script';
      if (npc && npc.dir === undefined) npc.dir = 's';
      /* Face the player when spoken to. */
      if (npc && !npc.sign) {
        var back = { n: 's', s: 'n', e: 'w', w: 'e' };
        npc.dir = back[g.dir] || npc.dir;
      }
      stepScript();
    }

    function resumeScript() {
      if (script) { mode = 'script'; stepScript(); }
      else mode = 'walk';
    }

    function stepScript() {
      if (dialogue && dialogue.length) return;
      while (script && scriptIndex < script.length) {
        var step = script[scriptIndex++];

        if (typeof step === 'string') { showDialogue([step]); return; }

        if (step.require && !g.flags[step.require]) { endScript(); return; }
        if (step.end) { endScript(); return; }

        if (step.give) {
          AE.addItem(g, step.give, step.n || 1);
          var it = AE.item(step.give);
          showDialogue([g.name + ' received ' + (step.n > 1 ? step.n + ' ' + it.name + 's' : it.name) + '!']);
          return;
        }
        if (step.money !== undefined) { g.money = Math.max(0, g.money + step.money); continue; }
        if (step.flag) { g.flags[step.flag] = step.val === undefined ? true : step.val; continue; }
        if (step.badge) {
          if (g.badges.indexOf(step.badge) < 0) g.badges.push(step.badge);
          /* Sigils double as story flags so `once`/`requireFlag` can gate on
             them — without this a beaten Warden would never stand down. */
          g.flags[step.badge] = true;
          continue;
        }
        if (step.skill) {
          if (!AE.hasSkill(g, step.skill)) g.skills.push(step.skill);
          var sk = AE.FIELD_SKILLS.find(function (s) { return s.id === step.skill; });
          showDialogue(['Your team learned the field skill ' + sk.name + '!']);
          return;
        }
        if (step.heal) { AE.healParty(g); continue; }
        if (step.warp) { endScript(); AE.owWarp(step.warp.map, step.warp.x, step.warp.y); return; }

        if (step.battle) { startTrainerBattle(step.battle, scriptNPC); return; }
        if (step.wild) { startWild(step.wild.sp, step.wild.lvl); return; }

        if (step.starter) {
          AE.push(AE.StarterScene(function () { resumeScript(); }));
          mode = 'walk';
          return;
        }
        if (step.shop) {
          AE.push(AE.ShopScene(step.shop, function () { resumeScript(); }));
          mode = 'walk';
          return;
        }
      }
      endScript();
    }

    function endScript() {
      script = null; scriptIndex = 0; scriptNPC = null;
      if (!dialogue || !dialogue.length) mode = 'walk';
    }

    /* ---------------- update ---------------- */
    function update(dt) {
      if (mode === 'fade') {
        fade += fadeDir * dt / 190;
        if (fadeDir > 0 && fade >= 1) {
          fade = 1;
          if (fadeAction) { var a = fadeAction; fadeAction = null; a(); }
          fadeDir = -1;
        } else if (fadeDir < 0 && fade <= 0) {
          fade = 0; fadeDir = 0; mode = 'walk';
          checkTrainerSight();
        }
        return;
      }

      if (mode === 'prompt') {
        if (AE.Input.tap('left') || AE.Input.tap('right') || AE.Input.tap('up') || AE.Input.tap('down')) {
          prompt.index = prompt.index ? 0 : 1;
        }
        if (AE.Input.tap('b')) { var no = prompt.onNo; prompt = null; mode = script ? 'script' : 'walk'; if (no) no(); if (script) stepScript(); return; }
        if (AE.Input.tap('a')) {
          var yes = prompt.index === 0 ? prompt.onYes : prompt.onNo;
          prompt = null;
          mode = script ? 'script' : 'walk';
          if (yes) yes();
          if (script && mode === 'script' && (!dialogue || !dialogue.length)) stepScript();
        }
        return;
      }

      if (mode === 'script') {
        dialogueTimer += dt;
        if (dialogue && dialogue.length) {
          if (AE.Input.tap('a') && dialogueTimer > 120) {
            dialogue.shift();
            dialogueTimer = 0;
            if (!dialogue.length) {
              dialogue = null;
              if (script) stepScript(); else mode = 'walk';
            }
          }
          return;
        }
        if (script) stepScript(); else mode = 'walk';
        return;
      }

      /* --- walking --- */
      if (turning > 0) { turning -= dt; if (turning > 0) return; }

      if (moving) {
        progress += dt / (hopping ? STEP_MS * 1.6 : STEP_MS);
        animAcc += dt;
        if (animAcc > 110) { animAcc = 0; stepFrame ^= 1; }
        if (progress >= 1) {
          progress = 0; moving = false; hopping = false;
          onArrive();
        }
        return;
      }

      if (AE.Input.tap('start')) { AE.push(AE.MenuScene()); return; }
      if (AE.Input.tap('a')) { interact(); return; }

      if (AE.Input.isDown('up')) tryStep('n');
      else if (AE.Input.isDown('down')) tryStep('s');
      else if (AE.Input.isDown('left')) tryStep('w');
      else if (AE.Input.isDown('right')) tryStep('e');
      else stepFrame = 0;
    }

    /* ---------------- draw ---------------- */
    function camera() {
      var m = map();
      var px = moving ? fromX + (g.x - fromX) * progress : g.x;
      var py = moving ? fromY + (g.y - fromY) * progress : g.y;
      var cx = px * TS + TS / 2 - AE.W / 2;
      var cy = py * TS + TS / 2 - AE.H / 2;
      var maxX = m.w * TS - AE.W, maxY = m.h * TS - AE.H;
      cx = maxX <= 0 ? maxX / 2 : Math.max(0, Math.min(maxX, cx));
      cy = maxY <= 0 ? maxY / 2 : Math.max(0, Math.min(maxY, cy));
      return { x: Math.round(cx), y: Math.round(cy), px: px, py: py };
    }

    function draw(ctx, now) {
      var m = map(), cam = camera();

      var x0 = Math.floor(cam.x / TS), y0 = Math.floor(cam.y / TS);
      var x1 = Math.ceil((cam.x + AE.W) / TS), y1 = Math.ceil((cam.y + AE.H) / TS);

      ctx.fillStyle = m.indoor ? '#151a24' : '#1d2a1c';
      ctx.fillRect(0, 0, AE.W, AE.H);

      for (var y = y0; y <= y1; y++) {
        for (var x = x0; x <= x1; x++) {
          if (x < 0 || y < 0 || x >= m.w || y >= m.h) continue;
          AE.drawTile(ctx, m.grid[y][x], x * TS - cam.x, y * TS - cam.y, now);
        }
      }

      /* people, sorted so lower ones overlap higher ones */
      var actors = [];
      AE.visibleNPCs(g, g.map).forEach(function (n) {
        if (n.sign) return;
        actors.push({ y: n.y, draw: function () {
          AE.drawPerson(ctx, AE.PALETTES[n.pal] || AE.PALETTES.villager,
            n.x * TS - cam.x, n.y * TS - cam.y, n.dir || 's', 0);
        } });
      });
      actors.push({ y: cam.py, draw: function () {
        var sx = Math.round(cam.px * TS - cam.x);
        var sy = Math.round(cam.py * TS - cam.y);
        if (hopping && moving) sy -= Math.round(Math.sin(progress * Math.PI) * 10);
        if (surfing) {
          ctx.fillStyle = 'rgba(140,205,255,.55)';
          ctx.beginPath(); ctx.ellipse(sx + 8, sy + 13, 9, 5, 0, 0, 6.284); ctx.fill();
        }
        AE.drawPerson(ctx, AE.PALETTES.player, sx, sy, g.dir, moving ? stepFrame : 0);
      } });
      actors.sort(function (a, b) { return a.y - b.y; });
      actors.forEach(function (a) { a.draw(); });

      /* caves are lit only around the player */
      if (m.dark) {
        var lx = Math.round(cam.px * TS - cam.x) + 8;
        var ly = Math.round(cam.py * TS - cam.y) + 8;
        var rg = ctx.createRadialGradient(lx, ly, 18, lx, ly, 86);
        rg.addColorStop(0, 'rgba(0,0,0,0)');
        rg.addColorStop(1, 'rgba(0,0,0,.86)');
        ctx.fillStyle = rg;
        ctx.fillRect(0, 0, AE.W, AE.H);
      }

      /* location banner while walking */
      if (mode === 'walk') {
        AE.text(ctx, m.name, 8, 6, { size: 10, bold: true, color: '#eaf2ff' });
        if (surfing) AE.text(ctx, 'Surging', 8, 20, { size: 9, color: '#9fe0ff' });
      }

      if (dialogue && dialogue.length) drawDialogue(ctx, dialogue[0]);
      if (mode === 'prompt' && prompt) drawPrompt(ctx);

      if (fade > 0) {
        ctx.fillStyle = 'rgba(0,0,0,' + Math.min(1, fade) + ')';
        ctx.fillRect(0, 0, AE.W, AE.H);
      }
    }

    function drawDialogue(ctx, text) {
      var y = AE.H - 66;
      AE.panel(ctx, 4, y, AE.W - 8, 62);
      var lines = AE.wrap(ctx, text, AE.W - 26, 11);
      for (var i = 0; i < Math.min(3, lines.length); i++) {
        AE.text(ctx, lines[i], 12, y + 10 + i * 15, { size: 11 });
      }
      AE.text(ctx, '▼', AE.W - 20, y + 46, { size: 9, color: '#8fd0ff' });
    }

    function drawPrompt(ctx) {
      var y = AE.H - 66;
      AE.panel(ctx, 4, y, AE.W - 8, 62);
      var lines = AE.wrap(ctx, prompt.text, AE.W - 26, 11);
      for (var i = 0; i < Math.min(2, lines.length); i++) {
        AE.text(ctx, lines[i], 12, y + 8 + i * 14, { size: 11 });
      }
      ['Yes', 'No'].forEach(function (label, i) {
        var on = prompt.index === i;
        AE.text(ctx, label, 26 + i * 70, y + 42, { size: 11, bold: on, color: on ? '#ffd84a' : '#dbe4f2' });
        if (on) AE.text(ctx, '▶', 14 + i * 70, y + 42, { size: 9, color: '#ffd84a' });
      });
    }

    return {
      isOverworld: true,
      update: update,
      draw: draw,
      onEnter: function () { AE.applyCleared(g); },
      onResume: function () { mode = script ? 'script' : 'walk'; }
    };
  };

})(window.AE = window.AE || {});
