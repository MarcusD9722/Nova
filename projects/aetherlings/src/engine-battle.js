/* Aetherlings — battles.
   Gen-3 shaped: the same damage formula, stat stages, status behaviour, catch
   formula and escape formula, with the numbers rounded the same way.

   The whole battle is driven by a step queue. Anything that happens — a message,
   an HP drain, a sprite flash — is pushed as a step and consumed in order, which
   is what keeps the narration in sync with the state changes. */
(function (AE) {
  'use strict';

  /* ================= maths ================= */

  var STAGE_MUL = function (s) {
    return s >= 0 ? (2 + s) / 2 : 2 / (2 - s);
  };
  var ACC_MUL = function (s) {
    return s >= 0 ? (3 + s) / 3 : 3 / (3 - s);
  };

  /* Effective stat for a combatant, including stage changes and burn/paralysis. */
  function statWith(side, key) {
    var base = AE.calcStat(side.mon, key);
    var v = Math.floor(base * STAGE_MUL(side.stages[key] || 0));
    if (key === 'atk' && side.mon.status === 'burn') v = Math.floor(v / 2);
    if (key === 'spe' && side.mon.status === 'para') v = Math.floor(v / 4);
    return Math.max(1, v);
  }
  AE.statWith = statWith;

  AE.calcDamage = function (atk, def, move, forceCrit) {
    var eff = AE.effectiveness(move.type, AE.species(def.mon.sp).types);
    if (eff === 0) return { dmg: 0, eff: 0, crit: false };

    var critRate = move.eff.highCrit ? 8 : 16;
    var crit = forceCrit !== undefined ? forceCrit : (AE.rand(critRate) === 0);

    var physical = move.cat === 'phys';
    var aKey = physical ? 'atk' : 'spa';
    var dKey = physical ? 'def' : 'spd';

    var A = statWith(atk, aKey);
    var D = statWith(def, dKey);
    /* A critical hit ignores the defender's boosts and the attacker's drops. */
    if (crit) {
      if ((atk.stages[aKey] || 0) < 0) A = AE.calcStat(atk.mon, aKey);
      if ((def.stages[dKey] || 0) > 0) D = AE.calcStat(def.mon, dKey);
    }

    var L = atk.mon.lvl;
    var d = Math.floor(Math.floor(Math.floor(2 * L / 5 + 2) * move.pow * A / D) / 50) + 2;

    if (crit) d *= 2;
    if (AE.species(atk.mon.sp).types.indexOf(move.type) >= 0) d = Math.floor(d * 1.5);
    d = Math.floor(d * eff);
    d = Math.floor(d * (85 + AE.rand(16)) / 100);

    return { dmg: Math.max(1, d), eff: eff, crit: crit };
  };

  AE.accuracyCheck = function (atk, def, move) {
    if (!move.acc) return true; /* acc 0 means it cannot miss */
    var stage = (atk.stages.acc || 0) - (def.stages.eva || 0);
    stage = Math.max(-6, Math.min(6, stage));
    return Math.random() * 100 < move.acc * ACC_MUL(stage);
  };

  /* Gen-3 catch formula, shakes and all. */
  AE.catchAttempt = function (mon, ballMult) {
    var rate = AE.species(mon.sp).catch;
    if (ballMult >= 255) return { caught: true, shakes: 4 };

    var max = AE.maxHP(mon);
    var statusMul = 1;
    if (mon.status === 'sleep' || mon.status === 'freeze') statusMul = 2;
    else if (mon.status !== 'none') statusMul = 1.5;

    var a = ((3 * max - 2 * mon.hp) * rate * ballMult) / (3 * max) * statusMul;
    a = Math.floor(a);
    if (a >= 255) return { caught: true, shakes: 4 };

    var b = Math.floor(1048560 / Math.floor(Math.sqrt(Math.floor(Math.sqrt(Math.floor(16711680 / a))))));
    var shakes = 0;
    for (var i = 0; i < 4; i++) {
      if (AE.rand(65536) < b) shakes++;
      else return { caught: false, shakes: shakes };
    }
    return { caught: true, shakes: 4 };
  };

  AE.escapeChance = function (playerSide, foeSide, attempts) {
    var a = statWith(playerSide, 'spe'), b = statWith(foeSide, 'spe');
    if (b <= 0) return true;
    var f = Math.floor((a * 128) / b) + 30 * attempts;
    if (f >= 256) return true;
    return AE.rand(256) < f;
  };

  AE.expGain = function (foe, isTrainer) {
    var base = AE.species(foe.sp).exp;
    return Math.max(1, Math.floor(base * foe.lvl / 7 * (isTrainer ? 1.5 : 1)));
  };

  AE.buildTrainerParty = function (t, g) {
    var list = t.dynamic ? t.dynamic(g) : t.party;
    return list.map(function (e) {
      return AE.makeMon(e.sp, e.lvl, { perfect: false });
    });
  };

  /* ================= battle scene ================= */

  function makeSide(mon) {
    return {
      mon: mon,
      stages: { atk: 0, def: 0, spa: 0, spd: 0, spe: 0, acc: 0, eva: 0 },
      conf: 0, flinch: false, shake: 0, flash: 0,
      shownHP: mon ? mon.hp : 0
    };
  }

  AE.BattleScene = function (cfg) {
    var g = AE.game;
    var isTrainer = !!cfg.trainer;
    var foeParty = isTrainer ? AE.buildTrainerParty(cfg.trainer, g) : [cfg.foe];
    var foeIndex = 0;

    var player = makeSide(g.party[AE.firstHealthy(g)]);
    var foe = makeSide(foeParty[0]);

    var queue = [];
    var state = 'queue';         /* queue | menu | fight | bag | party | over */
    var menuIndex = 0, fightIndex = 0, partyIndex = 0, bagPocket = 0, bagIndex = 0;
    var msg = null, msgTimer = 0, msgLines = [], autoAdvance = false;
    var runAttempts = 0, result = null, ended = false;
    var participants = [];

    function struggleSlot() { return { id: 'struggle', pp: 1, maxpp: 1 }; }

    /* The moves the FIGHT menu offers. If every move is drained, the only
       option becomes Struggle — otherwise a spent team would deadlock. */
    function fightList() {
      var mv = player.mon.moves;
      return mv.some(function (m) { return m.pp > 0; }) ? mv : [struggleSlot()];
    }

    function markParticipant(mon) {
      if (participants.indexOf(mon) < 0) participants.push(mon);
    }
    markParticipant(player.mon);

    /* ---- step helpers ----
       Every step is tagged with a section: 'm0'/'m1' for the two combatants'
       moves, 'post' for end-of-turn upkeep and faint handling. A move that
       fizzles skips only its own section; a faint skips the remaining moves but
       must never skip 'post', or the battle would never resolve. */
    var section = 'post';
    function say(text, auto) { queue.push({ t: 'msg', text: text, auto: !!auto, sec: section }); }
    function act(fn) { queue.push({ t: 'fn', fn: fn, sec: section }); }
    function pause(ms) { queue.push({ t: 'wait', ms: ms, sec: section }); }

    function name(side) {
      return (side === foe && !isTrainer ? 'Wild ' : '') + AE.monName(side.mon);
    }
    function foeLabel() { return isTrainer ? AE.monName(foe.mon) : 'Wild ' + AE.monName(foe.mon); }

    /* ---- damage application ---- */
    function applyDamage(side, amount) {
      side.mon.hp = Math.max(0, side.mon.hp - amount);
      side.flash = 220;
      side.shake = 240;
    }

    function heal(side, amount) {
      side.mon.hp = Math.min(AE.maxHP(side.mon), side.mon.hp + amount);
    }

    function statChange(side, changes, who) {
      Object.keys(changes).forEach(function (k) {
        var delta = changes[k];
        var before = side.stages[k] || 0;
        var after = Math.max(-6, Math.min(6, before + delta));
        side.stages[k] = after;
        var label = k === 'acc' ? 'accuracy' : k === 'eva' ? 'evasion' : AE.STAT_NAME[k];
        if (after === before) {
          say(who + '\'s ' + label + ' won\'t go ' + (delta > 0 ? 'higher' : 'lower') + '!');
        } else {
          var word = delta > 0 ? (delta > 1 ? 'sharply rose' : 'rose') : (delta < -1 ? 'sharply fell' : 'fell');
          say(who + '\'s ' + label + ' ' + word + '!');
        }
      });
    }

    function inflict(side, kind, who) {
      if (side.mon.status !== 'none') return false;
      var types = AE.species(side.mon.sp).types;
      /* Simple immunities that players expect to hold. */
      if (kind === 'burn' && types.indexOf('Ember') >= 0) return false;
      if (kind === 'freeze' && types.indexOf('Frost') >= 0) return false;
      if (kind === 'poison' && (types.indexOf('Toxin') >= 0 || types.indexOf('Iron') >= 0)) return false;
      if (kind === 'para' && types.indexOf('Storm') >= 0) return false;

      side.mon.status = kind;
      if (kind === 'sleep') side.mon.sleepTurns = AE.randInt(1, 3);
      say(who + ' ' + ({
        burn: 'was burned!', poison: 'was poisoned!', para: 'was paralysed!',
        sleep: 'fell asleep!', freeze: 'was frozen solid!'
      })[kind]);
      return true;
    }

    /* ---- the core: one combatant using one move ---- */
    function useMove(atk, def, slot) {
      var atkName = name(atk), defName = name(def);
      var mv = AE.move(slot.id);

      act(function () {
        if (atk.mon.hp <= 0) { skipRest(); return; }
      });

      /* Pre-move status gates */
      act(function () {
        if (atk.mon.status === 'freeze') {
          if (AE.chance(20)) { atk.mon.status = 'none'; say(atkName + ' thawed out!', true); }
          else { say(atkName + ' is frozen solid!', true); skipTurn(); }
        } else if (atk.mon.status === 'sleep') {
          if (atk.mon.sleepTurns <= 0) { atk.mon.status = 'none'; say(atkName + ' woke up!', true); }
          else { atk.mon.sleepTurns--; say(atkName + ' is fast asleep.', true); skipTurn(); }
        }
      });

      act(function () {
        if (atk.flinch) { atk.flinch = false; say(atkName + ' flinched!', true); skipTurn(); }
      });

      act(function () {
        if (atk.conf > 0) {
          atk.conf--;
          if (atk.conf === 0) { say(atkName + ' snapped out of its confusion!', true); return; }
          say(atkName + ' is confused...', true);
          if (AE.chance(50)) {
            /* Typeless 40-power self-hit, as in Gen 3. */
            var selfDmg = Math.max(1, Math.floor(Math.floor(Math.floor(2 * atk.mon.lvl / 5 + 2) * 40 *
              statWith(atk, 'atk') / statWith(atk, 'def')) / 50) + 2);
            applyDamage(atk, selfDmg);
            say('It hurt itself in its confusion!', true);
            skipTurn();
          }
        }
      });

      act(function () {
        if (atk.mon.status === 'para' && AE.chance(25)) {
          say(atkName + ' is paralysed! It can\'t move!', true);
          skipTurn();
        }
      });

      act(function () { slot.pp = Math.max(0, slot.pp - 1); });
      say(atkName + ' used ' + mv.name + '!', true);

      if (mv.cat === 'status') {
        act(function () {
          if (!AE.accuracyCheck(atk, def, mv)) { say(atkName + '\'s attack missed!', true); skipTurn(); return; }
          var e = mv.eff;
          if (e.heal) {
            var amt = Math.floor(AE.maxHP(atk.mon) * e.heal);
            if (atk.mon.hp >= AE.maxHP(atk.mon)) say(atkName + '\'s HP is already full!', true);
            else { heal(atk, amt); say(atkName + ' regained health!', true); }
          }
          if (e.stat) {
            var target = e.stat.who === 'self' ? atk : def;
            statChange(target, e.stat.changes, e.stat.who === 'self' ? atkName : defName);
          }
          if (e.status) {
            if (!inflict(def, e.status.k, defName)) say('It had no effect...', true);
          }
          if (e.confuse) {
            if (def.conf > 0) say(defName + ' is already confused!', true);
            else { def.conf = AE.randInt(2, 5); say(defName + ' became confused!', true); }
          }
        });
        return;
      }

      /* Damaging move */
      act(function () {
        var eff = AE.effectiveness(mv.type, AE.species(def.mon.sp).types);
        if (eff === 0) { say('It doesn\'t affect ' + defName + '...', true); skipTurn(); return; }
        if (!AE.accuracyCheck(atk, def, mv)) { say(atkName + '\'s attack missed!', true); skipTurn(); return; }

        var hits = 1;
        if (mv.eff.multihit) hits = AE.randInt(mv.eff.multihit[0], mv.eff.multihit[1]);

        var total = 0, lastEff = 1, anyCrit = false;
        for (var i = 0; i < hits && def.mon.hp - total > 0; i++) {
          var r = AE.calcDamage(atk, def, mv);
          total += r.dmg;
          lastEff = r.eff;
          if (r.crit) anyCrit = true;
        }
        total = Math.min(total, def.mon.hp);
        applyDamage(def, total);

        if (hits > 1) say('Hit ' + hits + ' time' + (hits > 1 ? 's' : '') + '!', true);
        if (anyCrit) say('A critical hit!', true);
        var et = AE.effectivenessText(lastEff);
        if (et) say(et, true);

        /* Ember attacks thaw a frozen target. */
        if (def.mon.status === 'freeze' && mv.type === 'Ember') {
          def.mon.status = 'none';
          say(defName + ' thawed out!', true);
        }

        if (mv.eff.drain) {
          var back = Math.max(1, Math.floor(total * mv.eff.drain));
          heal(atk, back);
          say(atkName + ' drained health!', true);
        }
        if (mv.eff.recoil) {
          var rec = Math.max(1, Math.floor(total * mv.eff.recoil));
          applyDamage(atk, rec);
          say(atkName + ' is hit by recoil!', true);
        }
        if (mv.eff.status && def.mon.hp > 0 && AE.chance(mv.eff.status.chance)) {
          inflict(def, mv.eff.status.k, defName);
        }
        if (mv.eff.confuse && def.mon.hp > 0 && AE.chance(mv.eff.confuse.chance) && def.conf === 0) {
          def.conf = AE.randInt(2, 5);
          say(defName + ' became confused!', true);
        }
        if (mv.eff.stat && def.mon.hp > 0 && AE.chance(mv.eff.stat.chance || 100)) {
          var tgt = mv.eff.stat.who === 'self' ? atk : def;
          statChange(tgt, mv.eff.stat.changes, mv.eff.stat.who === 'self' ? atkName : defName);
        }
        if (mv.eff.flinch && def.mon.hp > 0 && AE.chance(mv.eff.flinch)) def.flinch = true;
      });
    }

    /* Drops queued steps belonging to the current move (used when a move fizzles). */
    var skipMarker = null;
    function skipTurn() { skipMarker = { mode: 'sec', sec: section }; }
    function skipRest() { skipMarker = { mode: 'moves' }; }

    /* ---- end-of-turn upkeep ---- */
    function upkeep(side) {
      var who = name(side);
      act(function () {
        if (side.mon.hp <= 0) return;
        if (side.mon.status === 'burn') {
          var d = Math.max(1, Math.floor(AE.maxHP(side.mon) / 16));
          applyDamage(side, d);
          say(who + ' is hurt by its burn!', true);
        } else if (side.mon.status === 'poison') {
          var p = Math.max(1, Math.floor(AE.maxHP(side.mon) / 8));
          applyDamage(side, p);
          say(who + ' is hurt by poison!', true);
        }
      });
    }

    /* ---- faint handling ---- */
    function checkFaints() {
      act(function () {
        if (foe.mon.hp <= 0) {
          say(foeLabel() + ' fainted!', true);
          awardExp();
          advanceFoe();
        }
      });
      act(function () {
        if (player.mon.hp <= 0) {
          say(AE.monName(player.mon) + ' fainted!', true);
          act(function () {
            if (!AE.partyAlive(g)) {
              say(g.name + ' has no aetherlings left!');
              say(g.name + ' hurried back to the last Hearth...');
              act(function () { finish('lose'); });
            } else {
              state = 'party';
              partyIndex = AE.firstHealthy(g);
              forcedSwitch = true;
            }
          });
        }
      });
    }

    function awardExp() {
      var amount = AE.expGain(foe.mon, isTrainer);
      var alive = participants.filter(function (m) { return m.hp > 0; });
      var share = Math.max(1, Math.floor(amount / Math.max(1, alive.length)));
      alive.forEach(function (mon) {
        var events = AE.giveExp(mon, share);
        events.forEach(function (ev) {
          if (ev.t === 'exp') say(AE.monName(mon) + ' gained ' + ev.amount + ' EXP!', true);
          else if (ev.t === 'level') say(AE.monName(mon) + ' grew to level ' + ev.lvl + '!');
          else if (ev.t === 'move') say(AE.monName(mon) + ' learned ' + AE.move(ev.move).name + '!');
          else if (ev.t === 'moveFull') {
            say(AE.monName(mon) + ' tried to learn ' + AE.move(ev.move).name + ',');
            say('but it already knows four moves.', true);
          } else if (ev.t === 'evolve') {
            (function (target, toId) {
              say('What? ' + AE.monName(target) + ' is changing!');
              act(function () { AE.evolveMon(target, toId); });
              act(function () {
                g.seen[toId] = true; g.caught[toId] = true;
                say(AE.species(toId).name + '! It evolved!');
              });
            })(mon, ev.to);
          }
        });
      });
    }

    function advanceFoe() {
      act(function () {
        foeIndex++;
        if (!isTrainer || foeIndex >= foeParty.length) {
          if (isTrainer) {
            say(cfg.trainer.name + ' is out of aetherlings!');
            (cfg.trainer.win || []).forEach(function (l) { say(l); });
            var prize = cfg.trainer.money || 0;
            act(function () { g.money += prize; });
            say(g.name + ' got ' + prize + ' coin!');
          }
          act(function () { finish('win'); });
        } else {
          var next = foeParty[foeIndex];
          say(cfg.trainer.name + ' sent out ' + AE.monName(next) + '!');
          act(function () {
            foe = makeSide(next);
            foe.shownHP = next.hp;
          });
        }
      });
    }

    /* ---- turn assembly ---- */
    function takeTurn(playerAction) {
      state = 'queue';
      skipMarker = null;

      var foeSlot = chooseFoeMove();
      var playerSlot = playerAction.type === 'move' ? playerAction.slot : null;

      var order = [];
      if (playerAction.type === 'move') {
        var pMv = AE.move(playerSlot.id), fMv = AE.move(foeSlot.id);
        var pFirst;
        if (pMv.pri !== fMv.pri) pFirst = pMv.pri > fMv.pri;
        else {
          var ps = statWith(player, 'spe'), fs = statWith(foe, 'spe');
          pFirst = ps === fs ? AE.chance(50) : ps > fs;
        }
        order = pFirst
          ? [[player, foe, playerSlot], [foe, player, foeSlot]]
          : [[foe, player, foeSlot], [player, foe, playerSlot]];
      } else {
        /* Item / switch resolves first, then the foe attacks. */
        order = [[foe, player, foeSlot]];
      }

      order.forEach(function (o, i) {
        section = 'm' + i;
        useMove(o[0], o[1], o[2]);
        act(function () {
          if (o[1].mon.hp <= 0 || o[0].mon.hp <= 0) skipRest();
        });
      });

      section = 'post';
      upkeep(player);
      upkeep(foe);
      checkFaints();
      act(function () { if (!ended && state === 'queue') state = 'menu'; });
    }

    /* Foe AI: score each move by expected damage, weight type advantage, and
       leave a little randomness so it isn't perfectly predictable. */
    function chooseFoeMove() {
      var usable = foe.mon.moves.filter(function (m) { return m.pp > 0; });
      if (!usable.length) return struggleSlot();

      var scored = usable.map(function (slot) {
        var mv = AE.move(slot.id);
        var score;
        if (mv.cat === 'status') {
          score = 18 + AE.rand(14);
          if (mv.eff.heal && foe.mon.hp > AE.maxHP(foe.mon) * 0.6) score = 4;
        } else {
          var eff = AE.effectiveness(mv.type, AE.species(player.mon.sp).types);
          var stab = AE.species(foe.mon.sp).types.indexOf(mv.type) >= 0 ? 1.5 : 1;
          score = mv.pow * eff * stab * (mv.acc ? mv.acc / 100 : 1) / 3;
          score += AE.rand(10);
        }
        return { slot: slot, score: score };
      });
      scored.sort(function (a, b) { return b.score - a.score; });
      /* Mostly the best move, sometimes the second-best. */
      if (scored.length > 1 && AE.chance(22)) return scored[1].slot;
      return scored[0].slot;
    }

    /* ---- items and catching ---- */
    function useItem(id) {
      var item = AE.item(id);
      state = 'queue';

      if (item.use.catch !== undefined) {
        if (isTrainer) {
          say('You can\'t bind another tamer\'s aetherling!');
          act(function () { state = 'menu'; });
          return;
        }
        act(function () { AE.removeItem(g, id); });
        say(g.name + ' threw a ' + item.name + '!', true);
        act(function () {
          var r = AE.catchAttempt(foe.mon, item.use.catch);
          for (var i = 0; i < r.shakes && !r.caught; i++) say('...', true);
          if (r.caught) {
            say('Gotcha! ' + AE.monName(foe.mon) + ' was bound!');
            act(function () {
              g.caught[foe.mon.sp] = true;
              g.seen[foe.mon.sp] = true;
              foe.mon.met = AE.map(g.map).name;
              var where = AE.addToParty(g, foe.mon);
              if (where === 'box') say(AE.monName(foe.mon) + ' was sent to storage.');
              finish('caught');
            });
          } else {
            say(r.shakes === 0 ? 'Oh no! It broke free instantly!'
              : r.shakes >= 3 ? 'Aargh! So close!' : 'It broke free!', true);
            act(function () { takeTurn({ type: 'item' }); });
          }
        });
        return;
      }

      if (item.use.flee) {
        act(function () { AE.removeItem(g, id); });
        say(g.name + ' got away safely!');
        act(function () { finish('run'); });
        return;
      }

      var target = player.mon;
      var applied = false;

      if (item.use.heal || item.use.healAll) {
        var before = target.hp;
        var amt = item.use.healAll ? AE.maxHP(target) : item.use.heal;
        target.hp = Math.min(AE.maxHP(target), target.hp + amt);
        applied = target.hp !== before;
        if (applied) say(AE.monName(target) + ' recovered ' + (target.hp - before) + ' HP!', true);
      }
      if (item.use.cure) {
        if (item.use.cure === 'all' ? target.status !== 'none' : target.status === item.use.cure) {
          target.status = 'none'; target.sleepTurns = 0; applied = true;
          say(AE.monName(target) + ' was cured!', true);
        }
      }
      if (item.use.stat) {
        statChange(player, item.use.stat, AE.monName(target));
        applied = true;
      }
      if (item.use.pp) {
        target.moves.forEach(function (m) {
          if (item.use.pp === 'all') m.pp = m.maxpp;
        });
        if (item.use.pp !== 'all') {
          var slot = target.moves.find(function (m) { return m.pp < m.maxpp; });
          if (slot) slot.pp = Math.min(slot.maxpp, slot.pp + item.use.pp);
        }
        applied = true;
        say('PP was restored!', true);
      }

      if (!applied) {
        say('It won\'t have any effect.');
        act(function () { state = 'menu'; });
        return;
      }
      act(function () { AE.removeItem(g, id); });
      act(function () { takeTurn({ type: 'item' }); });
    }

    var forcedSwitch = false;

    function switchTo(index) {
      var mon = g.party[index];
      if (!mon || mon.hp <= 0 || mon === player.mon) return false;
      state = 'queue';
      var wasForced = forcedSwitch;
      forcedSwitch = false;
      say(AE.monName(player.mon) + ', come back!', true);
      act(function () {
        player = makeSide(mon);
        player.shownHP = mon.hp;
        markParticipant(mon);
      });
      say('Go, ' + AE.monName(mon) + '!', true);
      act(function () {
        if (wasForced) state = 'menu';
        else takeTurn({ type: 'switch' });
      });
      return true;
    }

    function tryRun() {
      if (isTrainer) {
        state = 'queue';
        say('There\'s no running from a tamer battle!');
        act(function () { state = 'menu'; });
        return;
      }
      state = 'queue';
      runAttempts++;
      if (AE.escapeChance(player, foe, runAttempts)) {
        say('Got away safely!');
        act(function () { finish('run'); });
      } else {
        say('Couldn\'t get away!', true);
        act(function () { takeTurn({ type: 'run' }); });
      }
    }

    function finish(r) {
      if (ended) return;
      ended = true;
      result = r;
      queue.length = 0;
      state = 'over';
      AE.pop();
      if (cfg.onEnd) cfg.onEnd(r);
    }

    /* ---- queue pump ---- */
    function pump(dt) {
      if (msg !== null) {
        msgTimer += dt;
        if (autoAdvance && msgTimer > 900) { msg = null; }
        else if (AE.Input.tap('a') && msgTimer > 120) { msg = null; }
        else return;
      }
      while (queue.length) {
        var step = queue.shift();
        if (skipMarker) {
          if (skipMarker.mode === 'sec' && step.sec === skipMarker.sec) continue;
          if (skipMarker.mode === 'moves' && step.sec !== 'post') continue;
          skipMarker = null;   /* reached a step outside the skipped scope */
        }
        section = step.sec || 'post';
        if (step.t === 'msg') {
          msg = step.text; msgTimer = 0; autoAdvance = step.auto;
          msgLines = null;
          return;
        }
        if (step.t === 'wait') { return; }
        if (step.t === 'fn') { step.fn(); if (ended) return; }
      }
      skipMarker = null;
      if (!ended && state === 'queue') state = 'menu';
    }

    /* ---- input ---- */
    function menuInput() {
      var opts = 4;
      if (AE.Input.tap('right') && menuIndex % 2 === 0) menuIndex++;
      if (AE.Input.tap('left') && menuIndex % 2 === 1) menuIndex--;
      if (AE.Input.tap('down') && menuIndex < 2) menuIndex += 2;
      if (AE.Input.tap('up') && menuIndex >= 2) menuIndex -= 2;
      menuIndex = (menuIndex + opts) % opts;

      if (AE.Input.tap('a')) {
        if (menuIndex === 0) { state = 'fight'; fightIndex = 0; }
        else if (menuIndex === 1) { state = 'party'; partyIndex = 0; }
        else if (menuIndex === 2) { state = 'bag'; bagPocket = 0; bagIndex = 0; }
        else tryRun();
      }
    }

    function fightInput() {
      var list = fightList(), n = list.length;
      if (fightIndex >= n) fightIndex = 0;
      if (AE.Input.tap('down')) fightIndex = (fightIndex + 1) % n;
      if (AE.Input.tap('up')) fightIndex = (fightIndex + n - 1) % n;
      if (AE.Input.tap('b')) { state = 'menu'; return; }
      if (AE.Input.tap('a')) {
        var slot = list[fightIndex];
        if (slot.pp <= 0) return;
        takeTurn({ type: 'move', slot: slot });
      }
    }

    function partyInput() {
      var n = g.party.length;
      if (AE.Input.tap('down')) partyIndex = (partyIndex + 1) % n;
      if (AE.Input.tap('up')) partyIndex = (partyIndex + n - 1) % n;
      if (AE.Input.tap('b') && !forcedSwitch) { state = 'menu'; return; }
      if (AE.Input.tap('a')) switchTo(partyIndex);
    }

    function bagList() {
      var pocket = AE.POCKETS[bagPocket];
      return Object.keys(g.bag).filter(function (id) {
        return AE.item(id).cat === pocket.id && g.bag[id] > 0 && !AE.item(id).use.key;
      });
    }

    function bagInput() {
      var list = bagList();
      if (AE.Input.tap('right')) { bagPocket = (bagPocket + 1) % 3; bagIndex = 0; }
      if (AE.Input.tap('left')) { bagPocket = (bagPocket + 2) % 3; bagIndex = 0; }
      if (list.length) {
        if (AE.Input.tap('down')) bagIndex = (bagIndex + 1) % list.length;
        if (AE.Input.tap('up')) bagIndex = (bagIndex + list.length - 1) % list.length;
      }
      if (AE.Input.tap('b')) { state = 'menu'; return; }
      if (AE.Input.tap('a') && list.length) useItem(list[bagIndex]);
    }

    /* ---- drawing ---- */
    function drawHPPanel(ctx, side, x, y, showExp) {
      var mon = side.mon, max = AE.maxHP(mon), w = 96;
      AE.panel(ctx, x, y, w, showExp ? 34 : 28);
      AE.text(ctx, AE.monName(mon), x + 6, y + 4, { size: 10, bold: true });
      AE.textRight(ctx, 'L' + mon.lvl, x + w - 6, y + 4, { size: 10 });

      var bx = x + 6, by = y + 17, bw = w - 12, bh = 5;
      ctx.fillStyle = '#2a3244'; ctx.fillRect(bx, by, bw, bh);
      var frac = Math.max(0, side.shownHP / max);
      ctx.fillStyle = frac > 0.5 ? '#57d98a' : frac > 0.2 ? '#f0c838' : '#ff5c72';
      ctx.fillRect(bx, by, Math.round(bw * frac), bh);
      ctx.strokeStyle = '#0d1018'; ctx.lineWidth = 1;
      ctx.strokeRect(bx + 0.5, by + 0.5, bw - 1, bh - 1);

      if (mon.status !== 'none') {
        var tag = AE.STATUS[mon.status].tag;
        ctx.fillStyle = '#c8503c';
        ctx.fillRect(x + 6, y + 24, 20, 8);
        AE.text(ctx, tag, x + 8, y + 24, { size: 8, bold: true });
      }
      if (showExp) {
        var ex = x + 6, ey = y + 26, ew = w - 12;
        ctx.fillStyle = '#2a3244'; ctx.fillRect(ex + (mon.status !== 'none' ? 22 : 0), ey, ew - (mon.status !== 'none' ? 22 : 0), 3);
        ctx.fillStyle = '#5aa9e6';
        ctx.fillRect(ex + (mon.status !== 'none' ? 22 : 0), ey,
          Math.round((ew - (mon.status !== 'none' ? 22 : 0)) * AE.expProgress(mon)), 3);
      }
    }

    function drawMessageBox(ctx, text) {
      var y = AE.H - 62;
      AE.panel(ctx, 4, y, AE.W - 8, 58);
      if (!text) return;
      var lines = AE.wrap(ctx, text, AE.W - 26, 11);
      for (var i = 0; i < Math.min(3, lines.length); i++) {
        AE.text(ctx, lines[i], 12, y + 10 + i * 14, { size: 11 });
      }
      if (!autoAdvance) {
        AE.text(ctx, '▼', AE.W - 20, y + 42, { size: 9, color: '#8fd0ff' });
      }
    }

    function drawMenu(ctx) {
      var y = AE.H - 62, labels = ['FIGHT', 'TEAM', 'BAG', 'RUN'];
      AE.panel(ctx, 4, y, AE.W - 8, 58);
      for (var i = 0; i < 4; i++) {
        var cx = 20 + (i % 2) * 110, cy = y + 12 + Math.floor(i / 2) * 22;
        var on = i === menuIndex;
        AE.text(ctx, labels[i], cx, cy, { size: 12, bold: on, color: on ? '#ffd84a' : '#dbe4f2' });
        if (on) AE.text(ctx, '▶', cx - 12, cy, { size: 10, color: '#ffd84a' });
      }
    }

    function drawFight(ctx) {
      var y = AE.H - 62;
      AE.panel(ctx, 4, y, AE.W - 8, 58);
      var moves = fightList();
      if (fightIndex >= moves.length) fightIndex = 0;
      for (var i = 0; i < moves.length; i++) {
        var mv = AE.move(moves[i].id);
        var on = i === fightIndex;
        var mx = 16 + (i % 2) * 110, my = y + 8 + Math.floor(i / 2) * 16;
        AE.text(ctx, mv.name, mx, my, { size: 10, bold: on, color: on ? '#ffd84a' : (moves[i].pp ? '#dbe4f2' : '#8a94a8') });
      }
      var sel = moves[fightIndex], selMv = AE.move(sel.id);
      ctx.fillStyle = AE.TYPE_COLOR[selMv.type];
      ctx.fillRect(12, y + 42, 42, 10);
      AE.text(ctx, selMv.type, 15, y + 42, { size: 8, bold: true, color: '#141821' });
      AE.text(ctx, 'PP ' + sel.pp + '/' + sel.maxpp, 62, y + 42, { size: 9 });
      AE.textRight(ctx, selMv.pow ? 'POW ' + selMv.pow : 'STATUS', AE.W - 14, y + 42, { size: 9 });
    }

    function drawPartyList(ctx) {
      AE.panel(ctx, 10, 40, AE.W - 20, AE.H - 110);
      AE.text(ctx, forcedSwitch ? 'Send out which?' : 'Switch to which?', 20, 48, { size: 11, bold: true });
      g.party.forEach(function (mon, i) {
        var y = 66 + i * 30, on = i === partyIndex;
        if (on) { ctx.fillStyle = 'rgba(255,216,74,.16)'; ctx.fillRect(14, y - 3, AE.W - 28, 28); }
        AE.drawCreature(ctx, mon.sp, 18, y - 4, 28, false);
        AE.text(ctx, AE.monName(mon), 50, y, { size: 10, bold: on, color: mon.hp > 0 ? '#fff' : '#ff8a9a' });
        AE.text(ctx, 'L' + mon.lvl + '  ' + mon.hp + '/' + AE.maxHP(mon), 50, y + 12, { size: 9, color: '#b9c6da' });
        if (mon === player.mon) AE.textRight(ctx, 'OUT', AE.W - 20, y, { size: 9, color: '#8fd0ff' });
        else if (mon.hp <= 0) AE.textRight(ctx, 'FNT', AE.W - 20, y, { size: 9, color: '#ff8a9a' });
      });
    }

    function drawBag(ctx) {
      AE.panel(ctx, 10, 40, AE.W - 20, AE.H - 110);
      var pocket = AE.POCKETS[bagPocket];
      AE.text(ctx, '◀ ' + pocket.name + ' ▶', 20, 48, { size: 11, bold: true });
      var list = bagList();
      if (!list.length) {
        AE.text(ctx, 'Nothing here.', 24, 74, { size: 10, color: '#9fb0c8' });
        return;
      }
      list.slice(0, 7).forEach(function (id, i) {
        var y = 70 + i * 20, on = i === bagIndex;
        if (on) { ctx.fillStyle = 'rgba(255,216,74,.16)'; ctx.fillRect(14, y - 3, AE.W - 28, 19); }
        AE.text(ctx, AE.item(id).name, 24, y, { size: 10, bold: on });
        AE.textRight(ctx, 'x' + g.bag[id], AE.W - 22, y, { size: 10, color: '#b9c6da' });
      });
    }

    /* ---- scene object ---- */
    return {
      isBattle: true,
      update: function (dt) {
        /* HP bars ease toward the real value so damage reads as motion. */
        [player, foe].forEach(function (s) {
          var target = s.mon.hp;
          if (s.shownHP > target) s.shownHP = Math.max(target, s.shownHP - Math.max(0.35, (s.shownHP - target) * 0.14));
          else if (s.shownHP < target) s.shownHP = Math.min(target, s.shownHP + Math.max(0.35, (target - s.shownHP) * 0.2));
          if (s.shake > 0) s.shake -= dt;
          if (s.flash > 0) s.flash -= dt;
        });

        if (state === 'queue') pump(dt);
        else if (msg !== null) pump(dt);
        else if (state === 'menu') menuInput();
        else if (state === 'fight') fightInput();
        else if (state === 'party') partyInput();
        else if (state === 'bag') bagInput();
      },

      draw: function (ctx, now) {
        /* backdrop */
        var grd = ctx.createLinearGradient(0, 0, 0, AE.H);
        grd.addColorStop(0, '#2a3a5e');
        grd.addColorStop(0.55, '#3c5a6e');
        grd.addColorStop(1, '#4a6a48');
        ctx.fillStyle = grd;
        ctx.fillRect(0, 0, AE.W, AE.H);

        ctx.fillStyle = 'rgba(30,52,36,.55)';
        ctx.beginPath(); ctx.ellipse(168, 118, 52, 14, 0, 0, 6.284); ctx.fill();
        ctx.beginPath(); ctx.ellipse(66, 214, 58, 16, 0, 0, 6.284); ctx.fill();

        /* foe */
        var fx = 138 + (foe.shake > 0 ? Math.sin(now / 22) * 3 : 0);
        ctx.save();
        if (foe.flash > 0 && Math.floor(now / 60) % 2 === 0) ctx.globalAlpha = 0.35;
        AE.drawCreature(ctx, foe.mon.sp, fx, 62, 62, false);
        ctx.restore();

        /* player */
        var px = 36 + (player.shake > 0 ? Math.sin(now / 22) * 3 : 0);
        ctx.save();
        if (player.flash > 0 && Math.floor(now / 60) % 2 === 0) ctx.globalAlpha = 0.35;
        AE.drawCreature(ctx, player.mon.sp, px, 152, 70, true);
        ctx.restore();

        drawHPPanel(ctx, foe, 8, 16, false);
        drawHPPanel(ctx, player, AE.W - 104, 168, true);

        if (!isTrainer) {
          AE.text(ctx, 'Wild', 8, 6, { size: 9, color: '#cfe0f5' });
        } else {
          AE.text(ctx, cfg.trainer.name, 8, 6, { size: 9, color: '#cfe0f5' });
          /* remaining party pips */
          for (var i = 0; i < foeParty.length; i++) {
            ctx.fillStyle = i < foeIndex ? '#5a6274' : (foeParty[i].hp > 0 ? '#ffd84a' : '#5a6274');
            ctx.fillRect(110 + i * 8, 8, 5, 5);
          }
        }

        if (msg !== null) drawMessageBox(ctx, msg);
        else if (state === 'menu') drawMenu(ctx);
        else if (state === 'fight') drawFight(ctx);
        else if (state === 'party') { drawMessageBox(ctx, ''); drawPartyList(ctx); }
        else if (state === 'bag') { drawMessageBox(ctx, ''); drawBag(ctx); }
        else drawMessageBox(ctx, '');
      },

      onEnter: function () {
        g.seen[foe.mon.sp] = true;
        if (isTrainer) {
          say(cfg.trainer.name + ' wants to battle!');
          (cfg.trainer.intro || []).forEach(function (l) { say(l); });
          say(cfg.trainer.name + ' sent out ' + AE.monName(foe.mon) + '!', true);
        } else {
          say('A wild ' + AE.monName(foe.mon) + ' appeared!', true);
        }
        say('Go, ' + AE.monName(player.mon) + '!', true);
        act(function () { state = 'menu'; });
      }
    };
  };

})(window.AE = window.AE || {});
