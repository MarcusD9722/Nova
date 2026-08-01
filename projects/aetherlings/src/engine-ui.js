/* Aetherlings — everything the player navigates outside of walking and battling:
   title, name entry, the starter choice, the pause menu, party, bag, storage,
   shops, field skills and saving. */
(function (AE) {
  'use strict';

  /* Shared cursor helper: wraps a vertical list and reports taps. */
  function vertical(len, idx) {
    if (!len) return 0;
    if (AE.Input.tap('down')) idx = (idx + 1) % len;
    if (AE.Input.tap('up')) idx = (idx + len - 1) % len;
    return Math.min(idx, len - 1);
  }

  function header(ctx, title) {
    ctx.fillStyle = '#131824';
    ctx.fillRect(0, 0, AE.W, AE.H);
    ctx.fillStyle = '#1e2740';
    ctx.fillRect(0, 0, AE.W, 26);
    AE.text(ctx, title, 10, 8, { size: 12, bold: true });
  }

  function footer(ctx, text) {
    AE.text(ctx, text, 10, AE.H - 16, { size: 9, color: '#8b9bb8' });
  }

  function typeChips(ctx, types, x, y) {
    types.forEach(function (t, i) {
      var w = 34;
      ctx.fillStyle = AE.TYPE_COLOR[t];
      ctx.fillRect(x + i * (w + 3), y, w, 10);
      AE.text(ctx, t, x + i * (w + 3) + 3, y + 1, { size: 8, bold: true, color: '#141821', shadow: false });
    });
  }

  function hpBar(ctx, mon, x, y, w) {
    var max = AE.maxHP(mon), frac = Math.max(0, mon.hp / max);
    ctx.fillStyle = '#2a3244'; ctx.fillRect(x, y, w, 4);
    ctx.fillStyle = frac > 0.5 ? '#57d98a' : frac > 0.2 ? '#f0c838' : '#ff5c72';
    ctx.fillRect(x, y, Math.round(w * frac), 4);
  }

  /* ===================== Title ===================== */
  AE.TitleScene = function () {
    var idx = 0, t = 0;
    var slots = [0, 1, 2];

    function summaries() { return slots.map(function (s) { return AE.saveSummary(s); }); }

    return {
      update: function (dt) {
        t += dt;
        idx = vertical(3, idx);
        if (AE.Input.tap('a')) {
          var sum = AE.saveSummary(idx);
          if (sum) {
            AE.game = AE.load(idx);
            AE.game.slot = idx;
            AE.applyCleared(AE.game);
            AE.replaceAll(AE.OverworldScene());
          } else {
            var slot = idx;
            AE.push(AE.NameScene(function (name) {
              AE.game = AE.newGame(name);
              AE.game.slot = slot;
              AE.save(AE.game, slot);
              AE.replaceAll(AE.OverworldScene());
            }));
          }
        }
      },
      draw: function (ctx) {
        var grd = ctx.createLinearGradient(0, 0, 0, AE.H);
        grd.addColorStop(0, '#1b2340');
        grd.addColorStop(1, '#2c4a3c');
        ctx.fillStyle = grd; ctx.fillRect(0, 0, AE.W, AE.H);

        AE.drawCreature(ctx, 3, 8, 34, 66, false);
        AE.drawCreature(ctx, 9, 168, 30, 68, false);
        AE.drawCreature(ctx, 6, 88, 18, 64, false);

        AE.text(ctx, 'AETHERLINGS', 30, 104, { size: 24, bold: true, color: '#ffd84a' });
        AE.text(ctx, 'a tamer\'s road through Verdane', 44, 130, { size: 10, color: '#cfe0f5' });

        summaries().forEach(function (sum, i) {
          var y = 156 + i * 44, on = i === idx;
          AE.panel(ctx, 20, y, AE.W - 40, 38, { fill: on ? 'rgba(46,60,92,.95)' : 'rgba(22,26,38,.9)' });
          if (sum) {
            AE.text(ctx, sum.name, 30, y + 6, { size: 11, bold: true });
            AE.text(ctx, sum.badges + ' Sigils  ·  ' + sum.party + ' in team  ·  L' + sum.top,
              30, y + 20, { size: 9, color: '#b9c6da' });
            AE.textRight(ctx, sum.time, AE.W - 30, y + 6, { size: 9, color: '#8fd0ff' });
          } else {
            AE.text(ctx, 'Slot ' + (i + 1) + ' — New Game', 30, y + 13, { size: 11, color: '#cfe0f5' });
          }
          if (on) AE.text(ctx, '▶', 10, y + 13, { size: 11, color: '#ffd84a' });
        });

        if (Math.floor(t / 500) % 2 === 0) {
          AE.text(ctx, 'Press A', 96, AE.H - 22, { size: 9, color: '#9fb0c8' });
        }
      }
    };
  };

  /* ===================== Name entry ===================== */
  AE.NameScene = function (onDone) {
    var ROWS = [
      ['A', 'B', 'C', 'D', 'E', 'F'],
      ['G', 'H', 'I', 'J', 'K', 'L'],
      ['M', 'N', 'O', 'P', 'Q', 'R'],
      ['S', 'T', 'U', 'V', 'W', 'X'],
      ['Y', 'Z', '-', ' ', '<', 'OK']
    ];
    var r = 0, c = 0, name = '';

    return {
      update: function () {
        if (AE.Input.tap('down')) r = (r + 1) % ROWS.length;
        if (AE.Input.tap('up')) r = (r + ROWS.length - 1) % ROWS.length;
        if (AE.Input.tap('right')) c = (c + 1) % ROWS[r].length;
        if (AE.Input.tap('left')) c = (c + ROWS[r].length - 1) % ROWS[r].length;
        c = Math.min(c, ROWS[r].length - 1);

        if (AE.Input.tap('b')) { name = name.slice(0, -1); }
        if (AE.Input.tap('start')) { finish(); }
        if (AE.Input.tap('a')) {
          var k = ROWS[r][c];
          if (k === 'OK') finish();
          else if (k === '<') name = name.slice(0, -1);
          else if (name.length < 10) name += k;
        }
      },
      draw: function (ctx) {
        header(ctx, 'What is your name?');
        AE.panel(ctx, 20, 40, AE.W - 40, 30);
        AE.text(ctx, name || '—', 32, 50, { size: 14, bold: true, color: '#ffd84a' });

        ROWS.forEach(function (row, ri) {
          row.forEach(function (k, ci) {
            var x = 22 + ci * 33, y = 92 + ri * 30;
            var on = ri === r && ci === c;
            AE.panel(ctx, x, y, k === 'OK' ? 34 : 28, 24,
              { fill: on ? 'rgba(255,216,74,.22)' : 'rgba(22,26,38,.9)' });
            AE.text(ctx, k === ' ' ? '␣' : k, x + (k === 'OK' ? 6 : 9), y + 6,
              { size: 11, bold: on, color: on ? '#ffd84a' : '#dbe4f2' });
          });
        });
        footer(ctx, 'A select · B backspace · START confirm');
      }
    };

    function finish() {
      AE.pop();
      onDone((name || 'Tamer').trim() || 'Tamer');
    }
  };

  /* ===================== Starter choice ===================== */
  AE.StarterScene = function (onDone) {
    var idx = 0, confirming = false;
    var ids = AE.STARTERS;

    return {
      update: function () {
        if (confirming) {
          if (AE.Input.tap('b')) { confirming = false; return; }
          if (AE.Input.tap('a')) {
            var g = AE.game, sp = ids[idx];
            var mon = AE.makeMon(sp, 5);
            AE.addToParty(g, mon);
            g.flags.starter = sp;
            g.seen[sp] = true; g.caught[sp] = true;
            AE.pop();
            onDone();
          }
          return;
        }
        if (AE.Input.tap('right')) idx = (idx + 1) % ids.length;
        if (AE.Input.tap('left')) idx = (idx + ids.length - 1) % ids.length;
        if (AE.Input.tap('a')) confirming = true;
      },
      draw: function (ctx) {
        header(ctx, 'Choose your partner');
        var sp = AE.species(ids[idx]);

        ids.forEach(function (id, i) {
          var x = 22 + i * 68, on = i === idx;
          AE.panel(ctx, x, 36, 60, 62, { fill: on ? 'rgba(255,216,74,.18)' : 'rgba(22,26,38,.9)' });
          AE.drawCreature(ctx, id, x + 6, 40, 48, false);
        });

        AE.panel(ctx, 14, 108, AE.W - 28, 120);
        AE.text(ctx, sp.name, 24, 116, { size: 14, bold: true, color: '#ffd84a' });
        typeChips(ctx, sp.types, 24, 136);
        var lines = AE.wrap(ctx, sp.dex, AE.W - 52, 10);
        lines.slice(0, 4).forEach(function (l, i) {
          AE.text(ctx, l, 24, 154 + i * 13, { size: 10, color: '#cfe0f5' });
        });
        AE.text(ctx, 'HP ' + sp.base.hp + '  ATK ' + sp.base.atk + '  DEF ' + sp.base.def,
          24, 206, { size: 9, color: '#9fb0c8' });

        if (confirming) {
          AE.panel(ctx, 30, 244, AE.W - 60, 46);
          AE.text(ctx, 'Take ' + sp.name + '?', 44, 254, { size: 12, bold: true });
          AE.text(ctx, 'A yes   ·   B choose again', 44, 272, { size: 9, color: '#9fb0c8' });
        } else {
          footer(ctx, '◀ ▶ browse · A choose');
        }
      }
    };
  };

  /* ===================== Pause menu ===================== */
  AE.MenuScene = function () {
    var items = ['Team', 'Bag', 'Storage', 'Skills', 'Tamer Card', 'Save', 'Close'];
    var idx = 0;

    return {
      update: function () {
        idx = vertical(items.length, idx);
        if (AE.Input.tap('b') || AE.Input.tap('start')) { AE.pop(); return; }
        if (AE.Input.tap('a')) {
          var pick = items[idx];
          if (pick === 'Team') AE.push(AE.PartyScene('view'));
          else if (pick === 'Bag') AE.push(AE.BagScene());
          else if (pick === 'Storage') AE.push(AE.StorageScene());
          else if (pick === 'Skills') AE.push(AE.SkillsScene());
          else if (pick === 'Tamer Card') AE.push(AE.CardScene());
          else if (pick === 'Save') AE.push(AE.SaveScene());
          else AE.pop();
        }
      },
      draw: function (ctx) {
        var w = 108, x = AE.W - w - 6;
        AE.panel(ctx, x, 8, w, items.length * 22 + 12);
        items.forEach(function (label, i) {
          var on = i === idx;
          AE.text(ctx, label, x + 18, 16 + i * 22, { size: 11, bold: on, color: on ? '#ffd84a' : '#dbe4f2' });
          if (on) AE.text(ctx, '▶', x + 6, 16 + i * 22, { size: 9, color: '#ffd84a' });
        });
      }
    };
  };

  /* ===================== Party ===================== */
  AE.PartyScene = function (mode, payload, onDone) {
    var g = AE.game;
    var idx = 0, detail = false, swapFrom = -1;
    var message = null, messageTimer = 0;

    function useItemOn(mon) {
      var item = AE.item(payload);
      var used = false, text = '';

      if (item.use.evoStone) {
        var sp = AE.species(mon.sp);
        if (sp.evo && sp.evo.stone === payload) {
          var toId = sp.evo.to;
          AE.evolveMon(mon, toId);
          g.seen[toId] = true; g.caught[toId] = true;
          used = true; text = 'It evolved into ' + AE.species(toId).name + '!';
        } else text = 'Nothing happened.';
      } else if (item.use.revive) {
        if (mon.hp > 0) text = AE.monName(mon) + ' is not fainted.';
        else {
          mon.hp = Math.max(1, Math.floor(AE.maxHP(mon) * item.use.revive));
          mon.status = 'none';
          used = true; text = AE.monName(mon) + ' was revived!';
        }
      } else if (item.use.heal || item.use.healAll) {
        if (mon.hp <= 0) text = 'It won\'t work on a fainted aetherling.';
        else if (mon.hp >= AE.maxHP(mon)) text = AE.monName(mon) + ' is already at full HP.';
        else {
          var before = mon.hp;
          mon.hp = Math.min(AE.maxHP(mon), mon.hp + (item.use.healAll ? AE.maxHP(mon) : item.use.heal));
          used = true; text = AE.monName(mon) + ' recovered ' + (mon.hp - before) + ' HP!';
        }
        if (used && item.use.cure === 'all') mon.status = 'none';
      } else if (item.use.cure) {
        if (item.use.cure === 'all' ? mon.status !== 'none' : mon.status === item.use.cure) {
          mon.status = 'none'; mon.sleepTurns = 0;
          used = true; text = AE.monName(mon) + ' was cured!';
        } else text = 'It won\'t have any effect.';
      } else if (item.use.pp) {
        var slot = mon.moves.find(function (m) { return m.pp < m.maxpp; });
        if (!slot) text = 'PP is already full.';
        else {
          if (item.use.pp === 'all') mon.moves.forEach(function (m) { m.pp = m.maxpp; });
          else slot.pp = Math.min(slot.maxpp, slot.pp + item.use.pp);
          used = true; text = 'PP was restored!';
        }
      } else text = 'It can\'t be used here.';

      if (used) AE.removeItem(g, payload);
      message = text; messageTimer = 0;
      if (used) setTimeout(function () {}, 0);
    }

    return {
      update: function (dt) {
        if (message !== null) {
          messageTimer += dt;
          if (AE.Input.tap('a') && messageTimer > 150) {
            message = null;
            if (mode === 'use') { AE.pop(); if (onDone) onDone(); }
          }
          return;
        }
        if (detail) {
          if (AE.Input.tap('b') || AE.Input.tap('a')) detail = false;
          if (AE.Input.tap('left')) idx = (idx + g.party.length - 1) % g.party.length;
          if (AE.Input.tap('right')) idx = (idx + 1) % g.party.length;
          return;
        }

        idx = vertical(g.party.length, idx);

        if (AE.Input.tap('b')) { AE.pop(); if (onDone) onDone(); return; }

        if (AE.Input.tap('start') && mode === 'view') {
          if (swapFrom < 0) swapFrom = idx;
          else {
            var tmp = g.party[swapFrom];
            g.party[swapFrom] = g.party[idx];
            g.party[idx] = tmp;
            swapFrom = -1;
          }
          return;
        }

        if (AE.Input.tap('a') && g.party.length) {
          if (mode === 'use') useItemOn(g.party[idx]);
          else detail = true;
        }
      },
      draw: function (ctx) {
        header(ctx, mode === 'use' ? 'Use on which?' : 'Your Team');

        /* Reachable before the Professor hands over a starter. */
        if (!g.party.length) {
          AE.text(ctx, 'You have no aetherlings yet.', 16, 48, { size: 10, color: '#8b9bb8' });
          footer(ctx, 'B back');
          return;
        }
        if (detail) { drawDetail(ctx, g.party[idx]); return; }

        g.party.forEach(function (mon, i) {
          var y = 32 + i * 44, on = i === idx;
          AE.panel(ctx, 8, y, AE.W - 16, 40,
            { fill: on ? 'rgba(46,60,92,.95)' : 'rgba(22,26,38,.9)',
              border: swapFrom === i ? '#ffd84a' : '#6f7fa8' });
          AE.drawCreature(ctx, mon.sp, 12, y + 2, 36, false);
          AE.text(ctx, AE.monName(mon), 52, y + 5, { size: 11, bold: true,
            color: mon.hp > 0 ? '#fff' : '#ff8a9a' });
          AE.text(ctx, 'L' + mon.lvl, 52, y + 19, { size: 9, color: '#b9c6da' });
          hpBar(ctx, mon, 84, y + 22, 74);
          AE.textRight(ctx, mon.hp + '/' + AE.maxHP(mon), AE.W - 22, y + 17, { size: 9, color: '#b9c6da' });
          if (mon.status !== 'none') {
            AE.textRight(ctx, AE.STATUS[mon.status].tag, AE.W - 22, y + 4, { size: 8, color: '#ff9a6a' });
          }
        });

        footer(ctx, mode === 'use' ? 'A use · B back'
          : (swapFrom >= 0 ? 'START to place · A summary' : 'A summary · START reorder · B back'));

        if (message !== null) {
          AE.panel(ctx, 12, AE.H - 60, AE.W - 24, 44);
          AE.wrap(ctx, message, AE.W - 44, 11).slice(0, 2).forEach(function (l, i) {
            AE.text(ctx, l, 22, AE.H - 50 + i * 14, { size: 11 });
          });
        }
      }
    };

    function drawDetail(ctx, mon) {
      var sp = AE.species(mon.sp);
      AE.drawCreature(ctx, mon.sp, 12, 30, 72, false);
      AE.text(ctx, AE.monName(mon), 94, 34, { size: 14, bold: true, color: '#ffd84a' });
      AE.text(ctx, 'Level ' + mon.lvl + '  ·  ' + mon.nature, 94, 52, { size: 9, color: '#b9c6da' });
      typeChips(ctx, sp.types, 94, 66);
      AE.text(ctx, 'HP ' + mon.hp + '/' + AE.maxHP(mon), 94, 82, { size: 10 });
      hpBar(ctx, mon, 94, 96, 130);

      var s = AE.stats(mon), keys = ['atk', 'def', 'spa', 'spd', 'spe'];
      keys.forEach(function (k, i) {
        var y = 112 + i * 15;
        AE.text(ctx, AE.STAT_NAME[k], 16, y, { size: 10, color: '#b9c6da' });
        AE.textRight(ctx, String(s[k]), 108, y, { size: 10, bold: true });
        ctx.fillStyle = '#2a3244'; ctx.fillRect(116, y + 3, 108, 5);
        ctx.fillStyle = '#5aa9e6';
        ctx.fillRect(116, y + 3, Math.min(108, Math.round(s[k] / 200 * 108)), 5);
      });

      AE.text(ctx, 'Moves', 16, 194, { size: 10, bold: true, color: '#8fd0ff' });
      mon.moves.forEach(function (slot, i) {
        var mv = AE.move(slot.id), y = 210 + i * 20;
        ctx.fillStyle = AE.TYPE_COLOR[mv.type];
        ctx.fillRect(16, y + 1, 8, 8);
        AE.text(ctx, mv.name, 30, y, { size: 10 });
        AE.textRight(ctx, slot.pp + '/' + slot.maxpp, AE.W - 20, y, { size: 9, color: '#b9c6da' });
      });

      var exp = AE.expToNext(mon);
      AE.text(ctx, mon.lvl >= 100 ? 'Max level' : exp + ' EXP to next level',
        16, AE.H - 32, { size: 9, color: '#8b9bb8' });
      footer(ctx, '◀ ▶ other team members · B back');
    }
  };

  /* ===================== Bag ===================== */
  AE.BagScene = function () {
    var g = AE.game;
    var pocket = 0, idx = 0, message = null, messageTimer = 0;

    function list() {
      var id = AE.POCKETS[pocket].id;
      return Object.keys(g.bag).filter(function (k) {
        return AE.item(k).cat === id && g.bag[k] > 0;
      });
    }

    return {
      update: function (dt) {
        if (message !== null) {
          messageTimer += dt;
          if (AE.Input.tap('a') && messageTimer > 150) message = null;
          return;
        }
        if (AE.Input.tap('right')) { pocket = (pocket + 1) % AE.POCKETS.length; idx = 0; }
        if (AE.Input.tap('left')) { pocket = (pocket + AE.POCKETS.length - 1) % AE.POCKETS.length; idx = 0; }
        var items = list();
        idx = vertical(items.length, idx);
        if (AE.Input.tap('b')) { AE.pop(); return; }
        if (AE.Input.tap('a') && items.length) {
          var id = items[idx], item = AE.item(id);
          if (item.use.key) { message = item.desc; messageTimer = 0; return; }
          if (item.use.battleOnly || item.use.catch !== undefined) {
            message = 'That can only be used in a battle.'; messageTimer = 0; return;
          }
          if (item.use.sell) { message = item.desc; messageTimer = 0; return; }
          AE.push(AE.PartyScene('use', id));
        }
      },
      draw: function (ctx) {
        header(ctx, 'Bag');
        AE.text(ctx, '◀ ' + AE.POCKETS[pocket].name + ' ▶', 10, 32, { size: 11, bold: true, color: '#8fd0ff' });
        AE.textRight(ctx, g.money + ' coin', AE.W - 10, 32, { size: 10, color: '#ffd84a' });

        var items = list();
        if (!items.length) AE.text(ctx, 'Nothing in this pocket.', 16, 60, { size: 10, color: '#8b9bb8' });

        items.slice(0, 9).forEach(function (id, i) {
          var y = 54 + i * 22, on = i === idx;
          if (on) { ctx.fillStyle = 'rgba(255,216,74,.14)'; ctx.fillRect(6, y - 3, AE.W - 12, 21); }
          AE.text(ctx, AE.item(id).name, 16, y, { size: 11, bold: on });
          AE.textRight(ctx, 'x' + g.bag[id], AE.W - 16, y, { size: 10, color: '#b9c6da' });
        });

        if (items.length) {
          AE.panel(ctx, 8, AE.H - 62, AE.W - 16, 44);
          AE.wrap(ctx, AE.item(items[idx]).desc, AE.W - 36, 10).slice(0, 2).forEach(function (l, i) {
            AE.text(ctx, l, 18, AE.H - 54 + i * 13, { size: 10, color: '#cfe0f5' });
          });
        }
        footer(ctx, 'A use · ◀ ▶ pocket · B back');

        if (message !== null) {
          AE.panel(ctx, 20, 120, AE.W - 40, 50);
          AE.wrap(ctx, message, AE.W - 60, 11).slice(0, 3).forEach(function (l, i) {
            AE.text(ctx, l, 30, 130 + i * 14, { size: 11 });
          });
        }
      }
    };
  };

  /* ===================== Storage ===================== */
  AE.StorageScene = function () {
    var g = AE.game;
    var col = 0, pIdx = 0, bIdx = 0, message = null, messageTimer = 0;

    return {
      update: function (dt) {
        if (message !== null) {
          messageTimer += dt;
          if (AE.Input.tap('a') && messageTimer > 150) message = null;
          return;
        }
        if (AE.Input.tap('left')) col = 0;
        if (AE.Input.tap('right')) col = 1;
        if (col === 0) pIdx = vertical(g.party.length, pIdx);
        else bIdx = vertical(g.box.length, bIdx);

        if (AE.Input.tap('b')) { AE.pop(); return; }
        if (AE.Input.tap('a')) {
          if (col === 0) {
            if (g.party.length <= 1) { message = 'You must keep at least one on your team.'; messageTimer = 0; return; }
            g.box.push(g.party.splice(pIdx, 1)[0]);
            pIdx = Math.max(0, pIdx - 1);
          } else {
            if (!g.box.length) return;
            if (g.party.length >= 6) { message = 'Your team is full.'; messageTimer = 0; return; }
            g.party.push(g.box.splice(bIdx, 1)[0]);
            bIdx = Math.max(0, bIdx - 1);
          }
        }
      },
      draw: function (ctx) {
        header(ctx, 'Storage');
        var mid = AE.W / 2;
        AE.text(ctx, 'Team', 14, 32, { size: 10, bold: col === 0, color: col === 0 ? '#ffd84a' : '#8b9bb8' });
        AE.text(ctx, 'Box', mid + 14, 32, { size: 10, bold: col === 1, color: col === 1 ? '#ffd84a' : '#8b9bb8' });

        g.party.forEach(function (mon, i) {
          var y = 50 + i * 26, on = col === 0 && i === pIdx;
          if (on) { ctx.fillStyle = 'rgba(255,216,74,.14)'; ctx.fillRect(6, y - 3, mid - 12, 24); }
          AE.drawCreature(ctx, mon.sp, 10, y - 3, 22, false);
          AE.text(ctx, AE.monName(mon), 34, y, { size: 9, bold: on });
          AE.text(ctx, 'L' + mon.lvl, 34, y + 11, { size: 8, color: '#b9c6da' });
        });

        if (!g.box.length) AE.text(ctx, 'Empty', mid + 14, 52, { size: 9, color: '#8b9bb8' });
        g.box.slice(0, 9).forEach(function (mon, i) {
          var y = 50 + i * 26, on = col === 1 && i === bIdx;
          if (on) { ctx.fillStyle = 'rgba(255,216,74,.14)'; ctx.fillRect(mid + 6, y - 3, mid - 12, 24); }
          AE.drawCreature(ctx, mon.sp, mid + 10, y - 3, 22, false);
          AE.text(ctx, AE.monName(mon), mid + 34, y, { size: 9, bold: on });
          AE.text(ctx, 'L' + mon.lvl, mid + 34, y + 11, { size: 8, color: '#b9c6da' });
        });

        footer(ctx, '◀ ▶ side · A move across · B back');

        if (message !== null) {
          AE.panel(ctx, 20, 130, AE.W - 40, 44);
          AE.wrap(ctx, message, AE.W - 60, 11).slice(0, 2).forEach(function (l, i) {
            AE.text(ctx, l, 30, 140 + i * 14, { size: 11 });
          });
        }
      }
    };
  };

  /* ===================== Field skills ===================== */
  AE.SkillsScene = function () {
    var g = AE.game;
    var idx = 0, choosingTown = false, townIdx = 0;

    function known() {
      return AE.FIELD_SKILLS.filter(function (s) { return AE.hasSkill(g, s.id); });
    }
    function towns() {
      return AE.TOWNS.filter(function (t) { return g.visited[t.map]; });
    }

    return {
      update: function () {
        if (choosingTown) {
          var list = towns();
          townIdx = vertical(list.length, townIdx);
          if (AE.Input.tap('b')) { choosingTown = false; return; }
          if (AE.Input.tap('a') && list.length) {
            var t = list[townIdx];
            AE.pop();                    /* skills */
            AE.pop();                    /* pause menu */
            AE.owWarp(t.map, t.x, t.y);
          }
          return;
        }
        var list = known();
        idx = vertical(list.length, idx);
        if (AE.Input.tap('b')) { AE.pop(); return; }
        if (AE.Input.tap('a') && list.length) {
          if (list[idx].id === 'recall') { choosingTown = true; townIdx = 0; }
        }
      },
      draw: function (ctx) {
        header(ctx, choosingTown ? 'Recall to where?' : 'Field Skills');
        if (choosingTown) {
          towns().forEach(function (t, i) {
            var y = 40 + i * 24, on = i === townIdx;
            if (on) { ctx.fillStyle = 'rgba(255,216,74,.14)'; ctx.fillRect(6, y - 3, AE.W - 12, 22); }
            AE.text(ctx, t.name, 20, y, { size: 11, bold: on });
          });
          footer(ctx, 'A travel · B back');
          return;
        }

        var list = known();
        if (!list.length) AE.text(ctx, 'You haven\'t learned any yet.', 16, 44, { size: 10, color: '#8b9bb8' });
        list.forEach(function (s, i) {
          var y = 40 + i * 34, on = i === idx;
          AE.panel(ctx, 8, y, AE.W - 16, 30, { fill: on ? 'rgba(46,60,92,.95)' : 'rgba(22,26,38,.9)' });
          AE.text(ctx, s.name, 18, y + 4, { size: 11, bold: true, color: '#ffd84a' });
          AE.text(ctx, 'Use it to ' + s.verb + '.', 18, y + 17, { size: 9, color: '#b9c6da' });
        });
        footer(ctx, list.length && list[idx].id === 'recall' ? 'A use · B back'
          : 'Used by walking up and pressing A · B back');
      }
    };
  };

  /* ===================== Tamer card ===================== */
  AE.CardScene = function () {
    var g = AE.game;
    var SIGILS = ['sigil1', 'sigil2', 'sigil3', 'sigil4', 'sigil5', 'sigil6', 'sigil7', 'sigil8'];
    var NAMES = ['Root', 'Ash', 'Tide', 'Storm', 'Dusk', 'Forge', 'Rime', 'Gale'];

    return {
      update: function () { if (AE.Input.tap('b') || AE.Input.tap('a')) AE.pop(); },
      draw: function (ctx) {
        header(ctx, 'Tamer Card');
        AE.panel(ctx, 10, 34, AE.W - 20, 78);
        AE.text(ctx, g.name, 22, 42, { size: 15, bold: true, color: '#ffd84a' });
        AE.text(ctx, 'Time played  ' + AE.formatTime(g.playtime), 22, 64, { size: 10, color: '#cfe0f5' });
        AE.text(ctx, 'Coin  ' + g.money, 22, 78, { size: 10, color: '#cfe0f5' });
        AE.text(ctx, 'Seen ' + Object.keys(g.seen).length + '  ·  Caught ' + Object.keys(g.caught).length +
          ' of ' + AE.DEX_LIST.length, 22, 92, { size: 10, color: '#cfe0f5' });

        AE.text(ctx, 'Sanctum Sigils', 16, 124, { size: 11, bold: true, color: '#8fd0ff' });
        SIGILS.forEach(function (id, i) {
          var x = 18 + (i % 4) * 54, y = 144 + Math.floor(i / 4) * 52;
          var got = g.badges.indexOf(id) >= 0;
          ctx.fillStyle = got ? '#ffd84a' : '#2a3244';
          ctx.beginPath(); ctx.arc(x + 18, y + 16, 15, 0, 6.284); ctx.fill();
          if (got) {
            ctx.fillStyle = '#a8791c';
            ctx.beginPath(); ctx.arc(x + 18, y + 16, 8, 0, 6.284); ctx.fill();
          }
          AE.text(ctx, NAMES[i], x + 4, y + 34, { size: 8, color: got ? '#ffd84a' : '#6b7789' });
        });

        if (g.flags.champion) {
          AE.text(ctx, 'CHAMPION OF VERDANE', 32, AE.H - 40, { size: 12, bold: true, color: '#ffd84a' });
        }
        footer(ctx, 'B back');
      }
    };
  };

  /* ===================== Save ===================== */
  AE.SaveScene = function () {
    var g = AE.game;
    var idx = g.slot || 0, message = null, messageTimer = 0;

    return {
      update: function (dt) {
        if (message !== null) {
          messageTimer += dt;
          if (AE.Input.tap('a') && messageTimer > 200) { message = null; AE.pop(); }
          return;
        }
        idx = vertical(3, idx);
        if (AE.Input.tap('b')) { AE.pop(); return; }
        if (AE.Input.tap('a')) {
          g.slot = idx;
          var ok = AE.save(g, idx);
          message = ok ? 'Saved to slot ' + (idx + 1) + '.'
            : 'Could not save. Storage is unavailable in this browser.';
          messageTimer = 0;
        }
      },
      draw: function (ctx) {
        header(ctx, 'Save');
        [0, 1, 2].forEach(function (s, i) {
          var sum = AE.saveSummary(s), y = 40 + i * 50, on = i === idx;
          AE.panel(ctx, 12, y, AE.W - 24, 44, { fill: on ? 'rgba(46,60,92,.95)' : 'rgba(22,26,38,.9)' });
          AE.text(ctx, 'Slot ' + (s + 1), 24, y + 6, { size: 11, bold: true });
          if (sum) {
            AE.text(ctx, sum.name + '  ·  ' + sum.badges + ' Sigils  ·  ' + sum.time,
              24, y + 22, { size: 9, color: '#b9c6da' });
          } else {
            AE.text(ctx, 'Empty', 24, y + 22, { size: 9, color: '#8b9bb8' });
          }
          if (s === g.slot) AE.textRight(ctx, 'current', AE.W - 24, y + 6, { size: 8, color: '#8fd0ff' });
        });
        footer(ctx, 'A save here · B back');

        if (message !== null) {
          AE.panel(ctx, 20, 200, AE.W - 40, 46);
          AE.wrap(ctx, message, AE.W - 60, 11).slice(0, 2).forEach(function (l, i) {
            AE.text(ctx, l, 30, 210 + i * 14, { size: 11 });
          });
        }
      }
    };
  };

  /* ===================== Shop ===================== */
  AE.ShopScene = function (shopId, onDone) {
    var g = AE.game;
    var stock = AE.SHOPS[shopId] || [];
    var tab = 0;            /* 0 buy, 1 sell */
    var idx = 0, qty = 1, message = null, messageTimer = 0;

    function sellable() {
      return Object.keys(g.bag).filter(function (id) {
        var it = AE.item(id);
        return g.bag[id] > 0 && !it.use.key && (it.price > 0 || it.use.sell);
      });
    }
    function sellPrice(id) {
      var it = AE.item(id);
      return it.use.sell || Math.floor(it.price / 2);
    }
    function current() { return tab === 0 ? stock : sellable(); }

    return {
      update: function (dt) {
        if (message !== null) {
          messageTimer += dt;
          if (AE.Input.tap('a') && messageTimer > 150) message = null;
          return;
        }
        if (AE.Input.tap('left') || AE.Input.tap('right')) {
          tab = tab ? 0 : 1; idx = 0; qty = 1;
        }
        var list = current();
        var before = idx;
        idx = vertical(list.length, idx);
        if (before !== idx) qty = 1;

        if (AE.Input.tap('b')) { AE.pop(); if (onDone) onDone(); return; }
        if (!list.length) return;

        var id = list[idx];
        if (AE.Input.tap('a')) {
          if (tab === 0) {
            var cost = AE.item(id).price * qty;
            if (cost > g.money) { message = 'You don\'t have enough coin.'; messageTimer = 0; return; }
            g.money -= cost;
            AE.addItem(g, id, qty);
            message = 'Bought ' + qty + ' ' + AE.item(id).name + '.';
          } else {
            var got = sellPrice(id) * qty;
            if ((g.bag[id] || 0) < qty) { message = 'You don\'t have that many.'; messageTimer = 0; return; }
            AE.removeItem(g, id, qty);
            g.money += got;
            message = 'Sold for ' + got + ' coin.';
          }
          messageTimer = 0; qty = 1;
        }
      },
      draw: function (ctx) {
        header(ctx, tab === 0 ? 'Buy' : 'Sell');
        AE.textRight(ctx, g.money + ' coin', AE.W - 10, 8, { size: 10, color: '#ffd84a' });
        AE.text(ctx, tab === 0 ? 'Buy  ◀▶  Sell' : 'Buy  ◀▶  Sell', 10, 32, { size: 9, color: '#8b9bb8' });

        var list = current();
        if (!list.length) AE.text(ctx, 'Nothing to show.', 16, 56, { size: 10, color: '#8b9bb8' });

        list.slice(0, 8).forEach(function (id, i) {
          var y = 50 + i * 22, on = i === idx, it = AE.item(id);
          if (on) { ctx.fillStyle = 'rgba(255,216,74,.14)'; ctx.fillRect(6, y - 3, AE.W - 12, 21); }
          AE.text(ctx, it.name, 16, y, { size: 11, bold: on });
          AE.textRight(ctx, (tab === 0 ? it.price : sellPrice(id)) + '', AE.W - 16, y, { size: 10, color: '#ffd84a' });
          if (tab === 1) AE.textRight(ctx, 'x' + g.bag[id], AE.W - 54, y, { size: 9, color: '#b9c6da' });
        });

        if (list.length) {
          AE.panel(ctx, 8, AE.H - 62, AE.W - 16, 44);
          AE.wrap(ctx, AE.item(list[idx]).desc, AE.W - 36, 10).slice(0, 2).forEach(function (l, i) {
            AE.text(ctx, l, 18, AE.H - 54 + i * 13, { size: 10, color: '#cfe0f5' });
          });
        }
        footer(ctx, 'A confirm · ◀ ▶ buy/sell · B leave');

        if (message !== null) {
          AE.panel(ctx, 20, 120, AE.W - 40, 46);
          AE.wrap(ctx, message, AE.W - 60, 11).slice(0, 2).forEach(function (l, i) {
            AE.text(ctx, l, 30, 130 + i * 14, { size: 11 });
          });
        }
      }
    };
  };

})(window.AE = window.AE || {});
