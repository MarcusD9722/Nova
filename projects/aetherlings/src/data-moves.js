/* Aetherlings — move list.
   cat: 'phys' | 'spec' | 'status'.  acc 0 means it never misses.
   eff keys the battle engine understands:
     status  {k, chance}            inflict burn/poison/para/sleep/freeze
     confuse {chance}
     stat    {who:'self'|'foe', changes:{atk:1,...}, chance}
     drain   fraction of damage dealt healed back
     recoil  fraction of damage dealt taken as recoil
     heal    fraction of the user's max HP restored
     flinch  chance the target loses its turn (only if the user moved first)
     multihit [min, max]
     highCrit true  */
(function (AE) {
  'use strict';

  function m(id, name, type, cat, pow, acc, pp, pri, eff, desc) {
    return { id: id, name: name, type: type, cat: cat, pow: pow, acc: acc,
             pp: pp, pri: pri || 0, eff: eff || {}, desc: desc || '' };
  }

  var LIST = [
    /* --- Beast --------------------------------------------------------- */
    m('ram', 'Ram', 'Beast', 'phys', 40, 100, 35, 0, {}, 'A plain full-body charge.'),
    m('rake', 'Rake', 'Beast', 'phys', 45, 100, 30, 0, {}, 'Slashes with claws or horns.'),
    m('dart', 'Dart', 'Beast', 'phys', 40, 100, 30, 1, {}, 'Always strikes first.'),
    m('pursue', 'Pursue', 'Beast', 'phys', 60, 100, 20, 0, {}, 'A dogged chasing blow.'),
    m('maul', 'Maul', 'Beast', 'phys', 80, 100, 20, 0, {}, 'A heavy mauling attack.'),
    m('bodycheck', 'Bodycheck', 'Beast', 'phys', 85, 100, 15, 0,
      { status: { k: 'para', chance: 30 } }, 'A body slam that may paralyse.'),
    m('crush', 'Crush', 'Beast', 'phys', 100, 85, 10, 0,
      { stat: { who: 'foe', changes: { def: -1 }, chance: 20 } }, 'May lower the foe\'s Defence.'),
    m('focus-blow', 'Focus Blow', 'Beast', 'phys', 120, 75, 5, 0, {}, 'Powerful but wildly inaccurate.'),
    m('final-fury', 'Final Fury', 'Beast', 'phys', 150, 90, 5, 0,
      { recoil: 0.33 }, 'Devastating, but the user is hurt badly.'),
    m('howl', 'Howl', 'Beast', 'status', 0, 0, 30, 0,
      { stat: { who: 'self', changes: { atk: 1 } } }, 'Raises the user\'s Attack.'),
    m('brace', 'Brace', 'Beast', 'status', 0, 0, 30, 0,
      { stat: { who: 'self', changes: { def: 1 } } }, 'Raises the user\'s Defence.'),
    m('glare', 'Glare', 'Beast', 'status', 0, 100, 20, 0,
      { stat: { who: 'foe', changes: { atk: -1 } } }, 'Lowers the foe\'s Attack.'),
    m('bed-down', 'Bed Down', 'Beast', 'status', 0, 0, 10, 0,
      { heal: 0.5 }, 'Restores half of the user\'s HP.'),

    /* --- Ember --------------------------------------------------------- */
    m('ember-spit', 'Ember Spit', 'Ember', 'spec', 40, 100, 25, 0,
      { status: { k: 'burn', chance: 10 } }, 'A small flame that may burn.'),
    m('cinder-lash', 'Cinder Lash', 'Ember', 'phys', 55, 100, 25, 0, {}, 'A whip of hot ash.'),
    m('flamewave', 'Flamewave', 'Ember', 'spec', 80, 100, 15, 0,
      { status: { k: 'burn', chance: 10 } }, 'A rolling wall of fire.'),
    m('magma-fang', 'Magma Fang', 'Ember', 'phys', 95, 95, 10, 0,
      { status: { k: 'burn', chance: 20 } }, 'A molten bite.'),
    m('pyre-blast', 'Pyre Blast', 'Ember', 'spec', 110, 85, 5, 0,
      { status: { k: 'burn', chance: 20 } }, 'An enormous column of flame.'),
    m('heat-haze', 'Heat Haze', 'Ember', 'status', 0, 100, 20, 0,
      { stat: { who: 'foe', changes: { acc: -1 } } }, 'Shimmering air spoils the foe\'s aim.'),
    m('sear', 'Sear', 'Ember', 'status', 0, 85, 15, 0,
      { status: { k: 'burn', chance: 100 } }, 'Burns the foe outright.'),

    /* --- Tide ---------------------------------------------------------- */
    m('splash-jet', 'Splash Jet', 'Tide', 'spec', 40, 100, 25, 0, {}, 'A quick spout of water.'),
    m('current', 'Current', 'Tide', 'spec', 65, 100, 20, 0, {}, 'A pulling undertow.'),
    m('brine-fang', 'Brine Fang', 'Tide', 'phys', 75, 100, 15, 0, {}, 'A salt-crusted bite.'),
    m('tidal-crash', 'Tidal Crash', 'Tide', 'spec', 95, 100, 10, 0, {}, 'A breaking wave.'),
    m('maelstrom', 'Maelstrom', 'Tide', 'spec', 120, 80, 5, 0, {}, 'A crushing whirlpool.'),
    m('mist-veil', 'Mist Veil', 'Tide', 'status', 0, 0, 20, 0,
      { stat: { who: 'self', changes: { def: 1, spd: 1 } } }, 'Raises both defences.'),
    m('drench', 'Drench', 'Tide', 'status', 0, 100, 20, 0,
      { stat: { who: 'foe', changes: { spe: -1 } } }, 'Sodden weight slows the foe.'),

    /* --- Verdant ------------------------------------------------------- */
    m('vine-whip', 'Vine Whip', 'Verdant', 'phys', 45, 100, 25, 0, {}, 'Lashes with a creeper.'),
    m('siphon-root', 'Siphon Root', 'Verdant', 'spec', 60, 100, 15, 0,
      { drain: 0.5 }, 'Heals the user by half the damage dealt.'),
    m('leaf-cut', 'Leaf Cut', 'Verdant', 'phys', 70, 100, 20, 0,
      { highCrit: true }, 'Razor leaves. High critical-hit rate.'),
    m('bloom-burst', 'Bloom Burst', 'Verdant', 'spec', 90, 100, 10, 0, {}, 'A blast of spores and petals.'),
    m('verdant-surge', 'Verdant Surge', 'Verdant', 'spec', 120, 85, 5, 0, {}, 'Roots erupt from below.'),
    m('spore-cloud', 'Spore Cloud', 'Verdant', 'status', 0, 75, 10, 0,
      { status: { k: 'sleep', chance: 100 } }, 'Puts the foe to sleep.'),
    m('barb-shield', 'Barb Shield', 'Verdant', 'status', 0, 0, 15, 0,
      { stat: { who: 'self', changes: { def: 2 } } }, 'Sharply raises Defence.'),
    m('renew', 'Renew', 'Verdant', 'status', 0, 0, 10, 0,
      { heal: 0.5 }, 'Draws on the World-Root to heal.'),

    /* --- Storm --------------------------------------------------------- */
    m('spark', 'Spark', 'Storm', 'spec', 45, 100, 25, 0,
      { status: { k: 'para', chance: 10 } }, 'A crackling jolt.'),
    m('jolt-bolt', 'Jolt Bolt', 'Storm', 'spec', 75, 100, 15, 0,
      { status: { k: 'para', chance: 10 } }, 'A forked bolt of lightning.'),
    m('static-fang', 'Static Fang', 'Storm', 'phys', 85, 95, 10, 0,
      { status: { k: 'para', chance: 20 } }, 'An electrified bite.'),
    m('thunderlance', 'Thunderlance', 'Storm', 'spec', 110, 80, 5, 0,
      { status: { k: 'para', chance: 20 } }, 'A spear of raw lightning.'),
    m('charge-up', 'Charge Up', 'Storm', 'status', 0, 0, 20, 0,
      { stat: { who: 'self', changes: { spa: 2 } } }, 'Sharply raises Sp. Atk.'),
    m('stun-net', 'Stun Net', 'Storm', 'status', 0, 90, 20, 0,
      { status: { k: 'para', chance: 100 } }, 'Paralyses the foe.'),

    /* --- Frost --------------------------------------------------------- */
    m('frostbite', 'Frostbite', 'Frost', 'phys', 50, 100, 25, 0,
      { status: { k: 'freeze', chance: 5 } }, 'A biting chill.'),
    m('icecap', 'Icecap', 'Frost', 'spec', 75, 100, 15, 0,
      { status: { k: 'freeze', chance: 10 } }, 'Encases the foe in rime.'),
    m('rime-slash', 'Rime Slash', 'Frost', 'phys', 80, 100, 15, 0,
      { highCrit: true }, 'An icicle blade. High critical-hit rate.'),
    m('glacier-fall', 'Glacier Fall', 'Frost', 'spec', 110, 80, 5, 0,
      { status: { k: 'freeze', chance: 10 } }, 'Drops a shelf of ice.'),
    m('chill-wind', 'Chill Wind', 'Frost', 'status', 0, 100, 20, 0,
      { stat: { who: 'foe', changes: { spe: -2 } } }, 'Sharply lowers the foe\'s Speed.'),

    /* --- Stone --------------------------------------------------------- */
    m('pebble-toss', 'Pebble Toss', 'Stone', 'phys', 45, 100, 25, 0, {}, 'Hurls loose stones.'),
    m('rock-slam', 'Rock Slam', 'Stone', 'phys', 75, 95, 20, 0, {}, 'A slab-swinging blow.'),
    m('quake', 'Quake', 'Stone', 'phys', 100, 100, 10, 0, {}, 'Shakes the whole battlefield.'),
    m('monolith-fall', 'Monolith Fall', 'Stone', 'phys', 130, 75, 5, 0, {}, 'Topples a standing stone.'),
    m('grit-storm', 'Grit Storm', 'Stone', 'status', 0, 100, 20, 0,
      { stat: { who: 'foe', changes: { acc: -1 } } }, 'Grit in the eyes lowers accuracy.'),
    m('harden-shell', 'Harden Shell', 'Stone', 'status', 0, 0, 15, 0,
      { stat: { who: 'self', changes: { def: 2 } } }, 'Sharply raises Defence.'),

    /* --- Gale ---------------------------------------------------------- */
    m('gust-flap', 'Gust Flap', 'Gale', 'spec', 45, 100, 30, 0, {}, 'A buffeting wingbeat.'),
    m('wing-slice', 'Wing Slice', 'Gale', 'phys', 70, 100, 20, 0, {}, 'A cutting pass at speed.'),
    m('featherstorm', 'Featherstorm', 'Gale', 'phys', 25, 90, 15, 0,
      { multihit: [2, 5] }, 'Strikes two to five times.'),
    m('cyclone', 'Cyclone', 'Gale', 'spec', 100, 90, 10, 0, {}, 'A spiralling column of wind.'),
    m('skydive', 'Skydive', 'Gale', 'phys', 115, 85, 5, 0, {}, 'A plunging dive from height.'),
    m('updraft', 'Updraft', 'Gale', 'status', 0, 0, 20, 0,
      { stat: { who: 'self', changes: { spe: 2 } } }, 'Sharply raises Speed.'),

    /* --- Toxin --------------------------------------------------------- */
    m('venom-jab', 'Venom Jab', 'Toxin', 'phys', 50, 100, 25, 0,
      { status: { k: 'poison', chance: 20 } }, 'A venomous stab.'),
    m('sludge-shot', 'Sludge Shot', 'Toxin', 'spec', 80, 100, 15, 0,
      { status: { k: 'poison', chance: 20 } }, 'Flings caustic sludge.'),
    m('blight-burst', 'Blight Burst', 'Toxin', 'spec', 110, 85, 5, 0,
      { status: { k: 'poison', chance: 30 } }, 'A bursting cloud of blight.'),
    m('toxic-mist', 'Toxic Mist', 'Toxin', 'status', 0, 90, 15, 0,
      { status: { k: 'poison', chance: 100 } }, 'Poisons the foe.'),
    m('corrode', 'Corrode', 'Toxin', 'status', 0, 100, 20, 0,
      { stat: { who: 'foe', changes: { def: -2 } } }, 'Sharply lowers the foe\'s Defence.'),

    /* --- Iron ---------------------------------------------------------- */
    m('iron-butt', 'Iron Butt', 'Iron', 'phys', 55, 100, 25, 0, {}, 'A headlong metal charge.'),
    m('magnet-pulse', 'Magnet Pulse', 'Iron', 'spec', 75, 100, 15, 0,
      { stat: { who: 'foe', changes: { spe: -1 }, chance: 20 } }, 'May slow the foe.'),
    m('steel-crush', 'Steel Crush', 'Iron', 'phys', 85, 95, 15, 0, {}, 'A vice-like crushing grip.'),
    m('forge-hammer', 'Forge Hammer', 'Iron', 'phys', 120, 80, 5, 0, {}, 'A hammer blow from the forge.'),
    m('plate-up', 'Plate Up', 'Iron', 'status', 0, 0, 20, 0,
      { stat: { who: 'self', changes: { def: 1, spd: 1 } } }, 'Raises both defences.'),

    /* --- Spirit -------------------------------------------------------- */
    m('mind-jab', 'Mind Jab', 'Spirit', 'spec', 50, 100, 25, 0, {}, 'A stab of raw thought.'),
    m('hex-drain', 'Hex Drain', 'Spirit', 'spec', 65, 100, 15, 0,
      { drain: 0.5 }, 'Heals the user by half the damage dealt.'),
    m('haunt', 'Haunt', 'Spirit', 'phys', 70, 100, 15, 0,
      { flinch: 20 }, 'May make the foe flinch.'),
    m('psywave', 'Psywave', 'Spirit', 'spec', 80, 100, 15, 0,
      { stat: { who: 'foe', changes: { spd: -1 }, chance: 20 } }, 'May lower Sp. Def.'),
    m('soul-rend', 'Soul Rend', 'Spirit', 'spec', 110, 85, 5, 0, {}, 'Tears at the spirit itself.'),
    m('focus-veil', 'Focus Veil', 'Spirit', 'status', 0, 0, 20, 0,
      { stat: { who: 'self', changes: { spa: 1, spd: 1 } } }, 'Raises Sp. Atk and Sp. Def.'),
    m('dreamsnare', 'Dreamsnare', 'Spirit', 'status', 0, 70, 10, 0,
      { status: { k: 'sleep', chance: 100 } }, 'Drags the foe into sleep.'),
    m('bewilder', 'Bewilder', 'Spirit', 'status', 0, 90, 15, 0,
      { confuse: { chance: 100 } }, 'Confuses the foe.'),

    /* --- Umbra --------------------------------------------------------- */
    m('shadow-nip', 'Shadow Nip', 'Umbra', 'phys', 50, 100, 25, 0, {}, 'A bite from your own shadow.'),
    m('gloom-pulse', 'Gloom Pulse', 'Umbra', 'spec', 85, 100, 15, 0,
      { stat: { who: 'foe', changes: { spa: -1 }, chance: 20 } }, 'May lower Sp. Atk.'),
    m('dusk-claw', 'Dusk Claw', 'Umbra', 'phys', 80, 100, 15, 0,
      { highCrit: true }, 'A darkened slash. High critical-hit rate.'),
    m('void-fang', 'Void Fang', 'Umbra', 'phys', 110, 85, 5, 0, {}, 'Jaws that close on nothing at all.'),
    m('shade-step', 'Shade Step', 'Umbra', 'status', 0, 0, 20, 0,
      { stat: { who: 'self', changes: { eva: 1 } } }, 'Raises evasion.'),
    m('terrify', 'Terrify', 'Umbra', 'status', 0, 100, 20, 0,
      { stat: { who: 'foe', changes: { spa: -2 } } }, 'Sharply lowers the foe\'s Sp. Atk.'),

    /* --- Signature ----------------------------------------------------- */
    m('worldroot', 'Worldroot', 'Verdant', 'spec', 130, 90, 5, 0, {},
      'Verdurion\'s own move. The ground itself answers.'),
    m('aether-beam', 'Aether Beam', 'Spirit', 'spec', 130, 90, 5, 0, {},
      'A lance of pure aether.'),

    /* Last resort when every move is out of PP. Not in any learnset — the
       battle engine substitutes it so a drained team can still act. */
    m('struggle', 'Struggle', 'Beast', 'phys', 50, 0, 1, 0,
      { recoil: 0.25 }, 'Used only when nothing else is left. It hurts the user too.')
  ];

  AE.MOVES = {};
  LIST.forEach(function (mv) { AE.MOVES[mv.id] = mv; });
  AE.MOVE_LIST = LIST;

  AE.move = function (id) {
    var mv = AE.MOVES[id];
    if (!mv) throw new Error('Unknown move: ' + id);
    return mv;
  };

})(window.AE = window.AE || {});
