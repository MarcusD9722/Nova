/* Aetherlings — people, dialogue, trainers and the story of Verdane.

   Script format (the overworld runs these):
     "some text"                     a line of dialogue
     { give: itemId, n: 1 }          add an item
     { money: 500 }                  add (or subtract) money
     { flag: 'name', val: true }     set a story flag
     { badge: 'sigil1' }             award a Sanctum Sigil
     { skill: 'cleave' }             grant a field skill
     { heal: true }                  restore the party
     { starter: true }               open the starter chooser
     { battle: <trainer> }           trainer battle
     { wild: { sp, lvl } }           scripted wild encounter
     { shop: 'shop-id' }             open a shop
     { warp: { map, x, y } }         move the player
     { require: 'flag' }             stop here unless the flag is set
     { end: true }                   stop the script early                       */
(function (AE) {
  'use strict';

  /* ---------------- trainer helpers ---------------- */
  function T(name, pal, party, money, intro, win, lose, sight) {
    return {
      name: name, pal: pal, party: party, money: money,
      intro: intro, win: win, lose: lose,
      sight: sight === undefined ? 4 : sight
    };
  }
  function p(sp, lvl) { return { sp: sp, lvl: lvl }; }

  /* Ferren mirrors your starter choice with the type that beats it. */
  function rivalStarter(g, stage) {
    var yours = (g && g.flags && g.flags.starter) || 1;
    var line = yours === 1 ? [4, 5, 6] : yours === 4 ? [7, 8, 9] : [1, 2, 3];
    return line[stage];
  }

  function rival(stage) {
    var cfg = [
      { lvl: 6, extra: [], money: 400, sight: 0 },
      { lvl: 14, extra: [p(10, 12)], money: 900, sight: 0 },
      { lvl: 23, extra: [p(11, 21), p(23, 21)], money: 1800, sight: 0 },
      { lvl: 32, extra: [p(11, 30), p(24, 30), p(33, 30)], money: 3000, sight: 0 },
      { lvl: 46, extra: [p(11, 43), p(25, 44), p(33, 43), p(49, 44)], money: 6000, sight: 0 }
    ][stage];
    var evoStage = stage === 0 ? 0 : stage <= 2 ? 1 : 2;

    return {
      name: 'Ferren', pal: 'rival', money: cfg.money, sight: cfg.sight,
      dynamic: function (g) {
        return cfg.extra.concat([p(rivalStarter(g, evoStage), cfg.lvl)]);
      },
      intro: [
        'Ferren: There you are.',
        'Ferren: Let\'s see how much of that was luck.'
      ],
      win: ['Ferren: ...Fine. That was skill.', 'Ferren: Next time it won\'t be.'],
      lose: ['Ferren: See? Told you.', 'Ferren: Go rest up. I\'ll wait.']
    };
  }

  /* ---------------- Sanctum Wardens ---------------- */
  var WARDENS = {
    thornhollow: {
      trainer: T('Warden Alder', 'warden',
        [p(17, 11), p(34, 12), p(18, 13)], 2000,
        ['Alder: Thornhollow grows slowly. So do good tamers.',
         'Alder: Show me you have grown at all.'],
        ['Alder: Ah. You have roots after all.'],
        ['Alder: Patience. Come back when you have some.']),
      sigil: 'sigil1', sigilName: 'Root Sigil', skill: 'cleave',
      after: ['Alder: The Root Sigil. It means the wild will listen a little.',
              'Alder: With it your team can Cleave through brush that blocks a path.']
    },
    cinderfall: {
      trainer: T('Warden Pyra', 'warden',
        [p(30, 16), p(35, 17), p(31, 18)], 3000,
        ['Pyra: Cinderfall doesn\'t do warm-ups.',
         'Pyra: Burn bright or go home.'],
        ['Pyra: Hah! You didn\'t flinch. Good.'],
        ['Pyra: Too slow. Come back hotter.']),
      sigil: 'sigil2', sigilName: 'Ash Sigil', skill: 'shatter',
      after: ['Pyra: Take the Ash Sigil. And this — Shatter.',
              'Pyra: Cracked boulders won\'t stop you any more.']
    },
    brackwater: {
      trainer: T('Warden Nerin', 'warden',
        [p(28, 21), p(43, 22), p(29, 23)], 4000,
        ['Nerin: The tide doesn\'t argue. It just keeps coming.',
         'Nerin: Let\'s see if you do.'],
        ['Nerin: You held. Not many hold.'],
        ['Nerin: The sea took that one. Try again.']),
      sigil: 'sigil3', sigilName: 'Tide Sigil', skill: 'recall',
      after: ['Nerin: The Tide Sigil, and a word of practical help.',
              'Nerin: Recall will carry you back to any town you have walked into.']
    },
    stormreach: {
      trainer: T('Warden Volsa', 'warden',
        [p(23, 26), p(47, 27), p(24, 28)], 5000,
        ['Volsa: Up here the sky decides things.',
         'Volsa: Today I decide things. Come on.'],
        ['Volsa: Ha! Struck clean through.'],
        ['Volsa: Grounded. Go on, off with you.']),
      sigil: 'sigil4', sigilName: 'Storm Sigil', skill: 'surge',
      after: ['Volsa: Storm Sigil, earned. And Surge with it.',
              'Volsa: Your team will carry you across open water now.']
    },
    gravemoor: {
      trainer: T('Warden Mourn', 'warden',
        [p(39, 30), p(52, 31), p(40, 32)], 6000,
        ['Mourn: Gravemoor remembers everyone who passes.',
         'Mourn: Let us see what it will remember of you.'],
        ['Mourn: It will remember this. So will I.'],
        ['Mourn: Not yet. The moor is patient; be patient with it.']),
      sigil: 'sigil5', sigilName: 'Dusk Sigil', skill: null,
      after: ['Mourn: The Dusk Sigil. Carry it quietly.']
    },
    ironhold: {
      trainer: T('Warden Gild', 'warden',
        [p(37, 34), p(22, 35), p(38, 36)], 7000,
        ['Gild: Everything in Ironhold gets tested before it gets used.',
         'Gild: You included.'],
        ['Gild: Sound. No cracks in you.'],
        ['Gild: Back to the forge with you.']),
      sigil: 'sigil6', sigilName: 'Forge Sigil', skill: 'ascend',
      after: ['Gild: Forge Sigil. And Ascend — the high cliffs are yours now.']
    },
    frostvale: {
      trainer: T('Warden Bryne', 'warden',
        [p(41, 38), p(45, 39), p(48, 39), p(42, 40)], 8000,
        ['Bryne: Cold is honest. It tells you exactly what you are.',
         'Bryne: Let\'s hear what it says about you.'],
        ['Bryne: Warm-blooded after all. Well done.'],
        ['Bryne: The cold had the last word. As usual.']),
      sigil: 'sigil7', sigilName: 'Rime Sigil', skill: null,
      after: ['Bryne: The Rime Sigil. You have earned the walk to Skyhaven.']
    },
    skyhaven: {
      trainer: T('Warden Kestrel', 'warden',
        [p(13, 42), p(49, 43), p(31, 43), p(50, 44)], 9000,
        ['Kestrel: Last Sanctum. After me there is only the Conclave.',
         'Kestrel: So this had better be worth watching.'],
        ['Kestrel: It was. Go on — they\'re expecting you.'],
        ['Kestrel: Not yet you don\'t. Come back up when you\'re ready.']),
      sigil: 'sigil8', sigilName: 'Gale Sigil', skill: null,
      after: ['Kestrel: Eight Sigils. The road to Aurel Citadel is open.',
              'Kestrel: ...Though I hear the Ashen Hand has been busy at the Spire.']
    }
  };
  AE.WARDENS = WARDENS;

  /* Builds the full Warden interaction, including the reward script. */
  function wardenScript(townId) {
    var w = WARDENS[townId];
    var post = [{ badge: w.sigil }];
    post.push('You received the ' + w.sigilName + '!');
    if (w.skill) post.push({ skill: w.skill });
    w.after.forEach(function (l) { post.push(l); });
    return { warden: w.trainer, post: post };
  }

  /* ---------------- NPCs, keyed by map ---------------- */
  var N = {};
  AE.NPCS = N;

  function sign(x, y, text) { return { x: x, y: y, sign: true, lines: [text] }; }
  function npc(x, y, dir, pal, name, lines, extra) {
    var o = { x: x, y: y, dir: dir, pal: pal, name: name, lines: lines };
    if (extra) for (var k in extra) o[k] = extra[k];
    return o;
  }

  /* Standard Hearth interior staff. */
  function hearthStaff(shopId) {
    return [
      npc(3, 4, 's', 'nurse', 'Attendant',
        ['Attendant: Welcome to the Hearth. Rest your team?'],
        { heal: true }),
      npc(11, 4, 's', 'clerk', 'Supplier',
        ['Supplier: Everything a tamer needs, more or less.'],
        { shop: shopId })
    ];
  }

  /* ===================== Willowmere ===================== */
  N['willowmere'] = [
    sign(4, 11, 'WILLOWMERE — where the road out begins.'),
    sign(17, 11, 'Rowan\'s Laboratory, south. Mind the ferns.'),
    npc(12, 12, 's', 'villager', 'Villager',
      ['Villager: Professor Rowan has been asking after you all morning.',
       'Villager: Something about a partner of your own.']),
    npc(24, 8, 'w', 'child', 'Kid',
      ['Kid: When I\'m older I\'m going to earn all eight Sigils.',
       'Kid: All of them. In one summer.']),
    npc(6, 19, 'e', 'elder', 'Elder Mae',
      ['Mae: The World-Root runs under all of Verdane, they say.',
       'Mae: Under this village too. It\'s why nothing here ever quite dies.'])
  ];

  N['home-willowmere'] = [
    npc(9, 5, 'w', 'villager2', 'Mum',
      ['Mum: Off to Rowan\'s, then?',
       'Mum: Take care of whichever one picks you. They choose too, you know.'])
  ];

  N['rival-home'] = [
    npc(4, 5, 'e', 'villager', 'Ferren\'s Dad',
      ['Ferren\'s Dad: He left before dawn. Of course he did.',
       'Ferren\'s Dad: Don\'t let him get too far ahead of you.'])
  ];

  N['lab-willowmere'] = [
    npc(8, 6, 's', 'prof', 'Professor Rowan',
      [
        'Rowan: There you are. Good.',
        'Rowan: Three aetherlings on that table, and none of them is mine to keep.',
        'Rowan: Go on. Choose.',
        { starter: true },
        'Rowan: A fine match. Truly.',
        { give: 'tamer-card' }, { give: 'sigil-case' }, { give: 'world-map' },
        { give: 'bindstone', n: 5 },
        'Rowan: Bindstones, five of them, and your Tamer Card.',
        'Rowan: Verdane has ten towns and eight Sanctums. Walk all of it.',
        { flag: 'gotStarter', val: true }
      ],
      { once: 'gotStarter' }),
    npc(8, 6, 's', 'prof', 'Professor Rowan',
      ['Rowan: Eight Sigils, then the Conclave at Aurel Citadel.',
       'Rowan: And keep an ear out. The Ashen Hand has been digging where it shouldn\'t.'],
      { requireFlag: 'gotStarter' }),
    npc(3, 4, 's', 'villager2', 'Assistant',
      ['Assistant: Wild aetherlings are weaker once you\'ve worn them down.',
       'Assistant: Lower their HP, then throw a Bindstone. Status helps too.'])
  ];

  N['hearth-willowmere'] = hearthStaff('shop-willowmere');

  /* Ferren blocks the north exit for your first battle. */
  N['route1'] = [
    sign(10, 30, 'ROUTE 1 — Willowmere to Thornhollow.'),
    sign(14, 9, 'Tall grass ahead. Wild aetherlings live in it.'),
    npc(12, 28, 's', 'rival', 'Ferren',
      [
        'Ferren: Knew you\'d come this way.',
        'Ferren: Rowan gave me one too. Let\'s settle it now.',
        { battle: rival(0) },
        { flag: 'beatRival1', val: true },
        'Ferren: Thornhollow\'s north of here. Warden Alder. Verdant type.',
        'Ferren: Try not to embarrass us both.'
      ],
      { once: 'beatRival1', blocksUntil: 'beatRival1' }),
    npc(6, 8, 'e', 'trainer', 'Forager Wend',
      ['Wend: You look like you\'re actually going somewhere. Prove it.'],
      { trainer: T('Forager Wend', 'trainer', [p(10, 4), p(17, 5)], 200,
        ['Wend: Let\'s see the new partner.'], ['Wend: Fair enough!'],
        ['Wend: Told you.']) }),
    npc(18, 15, 'w', 'trainer', 'Scout Peli',
      ['Peli: Two on two. Come on.'],
      { trainer: T('Scout Peli', 'trainer', [p(12, 5), p(10, 5)], 220,
        ['Peli: Quick round!'], ['Peli: You\'re quicker.'], ['Peli: Ha!']) })
  ];

  /* ===================== Thornhollow ===================== */
  var thorn = wardenScript('thornhollow');
  N['thornhollow'] = [
    sign(12, 12, 'THORNHOLLOW — the Verdant Sanctum stands east.'),
    sign(16, 20, 'Sanctum rule: challengers only. Everyone else, mind the hedges.'),
    npc(10, 13, 's', 'villager', 'Gardener',
      ['Gardener: Warden Alder grew that Sanctum from seed. Forty years.',
       'Gardener: He is not in a hurry and neither should you be.']),
    npc(20, 22, 'n', 'villager2', 'Traveller',
      ['Traveller: Brush too thick to pass? A Sanctum Sigil usually comes with a fix.'])
  ];
  N['hearth-thornhollow'] = hearthStaff('shop-thornhollow');
  N['thorn-house'] = [
    npc(9, 4, 'w', 'elder', 'Old Bram',
      ['Bram: Type matchups win battles, lad. Ember burns Verdant. Tide drowns Ember.',
       'Bram: Verdant drinks Tide. Round and round it goes.'])
  ];
  N['shop-thorn-house'] = [
    npc(6, 4, 's', 'clerk', 'Supplier',
      ['Supplier: Stock\'s better than Willowmere\'s, at least.'],
      { shop: 'shop-thornhollow' })
  ];
  N['sanctum-thornhollow'] = [
    npc(4, 10, 'e', 'trainer', 'Tender Rue',
      ['Rue: The Warden sees challengers who get past me.'],
      { trainer: T('Tender Rue', 'trainer', [p(17, 9), p(34, 10)], 500,
        ['Rue: Show me roots.'], ['Rue: Go on up.'], ['Rue: Not yet.']) }),
    npc(11, 10, 'w', 'trainer', 'Tender Cass',
      ['Cass: Same test, different hands.'],
      { trainer: T('Tender Cass', 'trainer', [p(14, 9), p(18, 10)], 500,
        ['Cass: Ready?'], ['Cass: Clean work.'], ['Cass: Ha!']) }),
    npc(7, 3, 's', 'warden', 'Warden Alder',
      thorn.warden.intro.concat([{ battle: thorn.warden }], thorn.post),
      { once: 'sigil1', warden: true }),
    npc(7, 3, 's', 'warden', 'Warden Alder',
      ['Alder: Cinderfall is east along Route 2. Warden Pyra. Ember.',
       'Alder: She will not go easy on you. She does not know how.'],
      { requireFlag: 'sigil1' })
  ];

  /* ===================== Route 2 / Emberdeep ===================== */
  N['route2'] = [
    sign(8, 10, 'ROUTE 2 — Cinderfall east. Emberdeep Cavern north.'),
    sign(30, 8, 'EMBERDEEP CAVERN — bring a light and a level head.'),
    npc(14, 8, 's', 'trainer', 'Hiker Dorn',
      ['Dorn: Long road. Might as well make it interesting.'],
      { trainer: T('Hiker Dorn', 'trainer', [p(20, 10), p(35, 11)], 600,
        ['Dorn: Rock and fire!'], ['Dorn: Solid win.'], ['Dorn: Hah!']) }),
    npc(22, 14, 'n', 'trainer', 'Catcher Ilse',
      ['Ilse: I catch more than I battle. But I do battle.'],
      { trainer: T('Catcher Ilse', 'trainer', [p(14, 10), p(32, 11), p(10, 11)], 620,
        ['Ilse: Three of mine!'], ['Ilse: Good team.'], ['Ilse: Told you.']) }),
    npc(36, 7, 'w', 'trainer', 'Ranger Tovi',
      ['Tovi: Cinderfall\'s just there. But you go through me first.'],
      { trainer: T('Ranger Tovi', 'trainer', [p(12, 11), p(17, 12)], 640,
        ['Tovi: Here we go!'], ['Tovi: Well fought.'], ['Tovi: Ha, no.']) })
  ];

  N['emberdeep'] = [
    npc(6, 5, 's', 'miner', 'Miner Hald',
      ['Hald: Cracked rock everywhere down here. Shame nobody can shift it.',
       'Hald: Warden Pyra can teach that trick, if you can beat her.']),
    npc(16, 22, 'w', 'trainer', 'Digger Vos',
      ['Vos: Nobody comes down here by accident.'],
      { trainer: T('Digger Vos', 'trainer', [p(20, 12), p(20, 12), p(35, 13)], 700,
        ['Vos: Stone holds!'], ['Vos: Hm. It didn\'t.'], ['Vos: Stone holds.']) })
  ];

  /* ===================== Cinderfall ===================== */
  var cinder = wardenScript('cinderfall');
  N['cinderfall'] = [
    sign(12, 14, 'CINDERFALL — built on warm ground. Sanctum to the southeast.'),
    sign(17, 7, 'Do not dig here. We mean it. — Town Council'),
    npc(11, 15, 's', 'villager2', 'Smith',
      ['Smith: Ground\'s warm all year. Never floods, never freezes.',
       'Smith: Never entirely safe either.']),
    npc(24, 6, 'w', 'villager', 'Watcher',
      ['Watcher: Folk in grey robes came through last week. Asked about the World-Root.',
       'Watcher: Didn\'t like the way they asked.'])
  ];
  N['hearth-cinderfall'] = hearthStaff('shop-cinderfall');
  N['cinder-shop'] = [
    npc(6, 4, 's', 'clerk', 'Supplier',
      ['Supplier: Runestones in now. They bind better than the plain ones.'],
      { shop: 'shop-cinderfall' })
  ];
  N['cinder-house'] = [
    npc(4, 4, 'e', 'elder', 'Retired Warden',
      ['Retired Warden: A burned aetherling loses HP each turn and hits softer.',
       'Retired Warden: Paralysis is worse. Half speed, and sometimes no turn at all.'])
  ];
  N['sanctum-cinderfall'] = [
    npc(4, 10, 'e', 'trainer', 'Stoker Bran',
      ['Bran: Hot enough for you?'],
      { trainer: T('Stoker Bran', 'trainer', [p(30, 14), p(35, 15)], 900,
        ['Bran: Light it up!'], ['Bran: Fair.'], ['Bran: Hah!']) }),
    npc(11, 10, 'w', 'trainer', 'Stoker Nell',
      ['Nell: Past me, then the Warden.'],
      { trainer: T('Stoker Nell', 'trainer', [p(4, 14), p(30, 15), p(20, 14)], 900,
        ['Nell: Come on!'], ['Nell: Good.'], ['Nell: Ha!']) }),
    npc(7, 3, 's', 'warden', 'Warden Pyra',
      cinder.warden.intro.concat([{ battle: cinder.warden }], cinder.post),
      { once: 'sigil2', warden: true }),
    npc(7, 3, 's', 'warden', 'Warden Pyra',
      ['Pyra: South to Brackwater next. Nerin\'s Sanctum. Tide.',
       'Pyra: She\'ll bore you to death before she beats you. Watch for it.'],
      { requireFlag: 'sigil2' })
  ];

  /* ===================== Route 3 / Mirewood — Ashen Hand appears ===================== */
  N['route3'] = [
    sign(14, 6, 'ROUTE 3 — Brackwater south. Mirewood west.'),
    sign(10, 20, 'MIREWOOD — footing poor. Aetherlings plentiful.'),
    npc(12, 12, 's', 'ashen', 'Ashen Adept',
      [
        'Ashen Adept: This road is closed. Hand business.',
        'Ashen Adept: ...You don\'t look like you\'re leaving.',
        { battle: T('Ashen Adept', 'ashen', [p(32, 14), p(39, 15)], 800,
          ['Adept: The Hand does not explain itself.'],
          ['Adept: ...Move along, then.'],
          ['Adept: As I said. Closed.']) },
        { flag: 'ashen1', val: true },
        'Ashen Adept: Fine. Walk it. It won\'t matter soon.',
        'Ashen Adept: The Root wakes whether you like it or not.'
      ],
      { once: 'ashen1', blocksUntil: 'ashen1' }),
    npc(6, 24, 'e', 'trainer', 'Wanderer Sel',
      ['Sel: Everyone\'s in a hurry today.'],
      { trainer: T('Wanderer Sel', 'trainer', [p(17, 15), p(34, 16)], 900,
        ['Sel: Slow down a minute.'], ['Sel: Off you go.'], ['Sel: Told you.']) }),
    npc(18, 16, 'w', 'trainer', 'Botanist Fen',
      ['Fen: Verdant types, obviously.'],
      { trainer: T('Botanist Fen', 'trainer', [p(18, 15), p(34, 16), p(17, 15)], 950,
        ['Fen: For science.'], ['Fen: Noted.'], ['Fen: Also noted.']) })
  ];

  N['mirewood'] = [
    sign(18, 8, 'MIREWOOD — the water here used to be clear.'),
    npc(8, 12, 's', 'ashen', 'Ashen Adept',
      ['Adept: Draining a mire is slow work. Don\'t make it slower.'],
      { trainer: T('Ashen Adept', 'ashen', [p(28, 16), p(32, 17), p(39, 16)], 1100,
        ['Adept: The Hand takes what it needs.'],
        ['Adept: ...Report this to whoever you like.'],
        ['Adept: Good.']) }),
    npc(16, 21, 'n', 'villager', 'Mire Warden',
      ['Mire Warden: They\'ve been pulling the water out for a month.',
       'Mire Warden: Say they\'re looking for a root. A big one.'])
  ];

  /* ===================== Brackwater ===================== */
  var brack = wardenScript('brackwater');
  N['brackwater'] = [
    sign(12, 10, 'BRACKWATER — harbour town. Sanctum by the water.'),
    sign(17, 17, 'Deep water beyond this point. Do not swim without a partner.'),
    npc(10, 11, 's', 'sailor', 'Sailor Onn',
      ['Onn: Can\'t cross open water without the right Sigil.',
       'Onn: Warden Volsa up in Stormreach hands that one out.']),
    npc(24, 17, 'w', 'villager2', 'Dockhand',
      ['Dockhand: Grey robes chartered a boat last week. Paid in old coin.',
       'Dockhand: Wouldn\'t say where they were going.'])
  ];
  N['hearth-brackwater'] = hearthStaff('shop-brackwater');
  N['brack-shop'] = [
    npc(6, 4, 's', 'clerk', 'Supplier',
      ['Supplier: Revives in stock. You\'ll want them.'],
      { shop: 'shop-brackwater' })
  ];
  N['brack-house'] = [
    npc(9, 4, 'w', 'elder', 'Old Salt',
      ['Old Salt: Switching partners mid-battle costs you a turn.',
       'Old Salt: Costs you the whole battle if you do it at the wrong moment.'])
  ];
  N['sanctum-brackwater'] = [
    npc(4, 10, 'e', 'trainer', 'Tidehand Mor',
      ['Mor: In you come.'],
      { trainer: T('Tidehand Mor', 'trainer', [p(28, 19), p(43, 20)], 1400,
        ['Mor: Steady.'], ['Mor: Steady enough.'], ['Mor: Steady.']) }),
    npc(11, 10, 'w', 'trainer', 'Tidehand Isa',
      ['Isa: Nerin is behind me. Good luck.'],
      { trainer: T('Tidehand Isa', 'trainer', [p(7, 19), p(28, 20), p(26, 19)], 1400,
        ['Isa: Here it comes.'], ['Isa: Well held.'], ['Isa: Ha!']) }),
    npc(7, 3, 's', 'warden', 'Warden Nerin',
      brack.warden.intro.concat([{ battle: brack.warden }], brack.post),
      { once: 'sigil3', warden: true }),
    npc(7, 3, 's', 'warden', 'Warden Nerin',
      ['Nerin: East along Route 4 for Stormreach.',
       'Nerin: And if you meet grey robes on the way — they are not travellers.'],
      { requireFlag: 'sigil3' })
  ];

  /* ===================== Route 4 / Stormreach ===================== */
  N['route4'] = [
    sign(12, 11, 'ROUTE 4 — Stormreach east. Mind the cliffs.'),
    sign(28, 9, 'Cliffs impassable without the right Sigil.'),
    npc(10, 6, 's', 'trainer', 'Drover Kell',
      ['Kell: Herd\'s ahead. You\'re not.'],
      { trainer: T('Drover Kell', 'trainer', [p(23, 19), p(12, 19)], 1200,
        ['Kell: Move!'], ['Kell: Aye, fair.'], ['Kell: Move along.']) }),
    npc(26, 16, 'n', 'trainer', 'Angler Bo',
      ['Bo: Nothing biting. Might as well.'],
      { trainer: T('Angler Bo', 'trainer', [p(43, 20), p(28, 20), p(7, 19)], 1250,
        ['Bo: Line\'s out.'], ['Bo: Nice one.'], ['Bo: Ha!']) }),
    npc(36, 6, 'w', 'trainer', 'Runner Vey',
      ['Vey: Race you. No? Battle, then.'],
      { trainer: T('Runner Vey', 'trainer', [p(13, 21), p(23, 21)], 1300,
        ['Vey: Go!'], ['Vey: Faster than me.'], ['Vey: Told you.']) })
  ];

  var storm = wardenScript('stormreach');
  N['stormreach'] = [
    sign(12, 9, 'STORMREACH — high plain. Storm Sanctum southeast.'),
    sign(17, 18, 'Lightning ground. Do not shelter under the lone trees.'),
    npc(10, 10, 's', 'villager', 'Herder',
      ['Herder: Thunderhoof come through twice a year. Whole plain shakes.']),
    npc(24, 22, 'n', 'villager2', 'Watcher',
      ['Watcher: Grey robes went north toward Gravemoor. Carrying digging tools.'])
  ];
  N['hearth-stormreach'] = hearthStaff('shop-stormreach');
  N['storm-shop'] = [
    npc(6, 4, 's', 'clerk', 'Supplier',
      ['Supplier: Charms boost a stat for one battle. Cheap edge.'],
      { shop: 'shop-stormreach' })
  ];
  N['storm-house'] = [
    npc(4, 4, 'e', 'elder', 'Weatherwatch',
      ['Weatherwatch: Storm attacks simply cannot touch a Stone type. Not weakly. At all.'])
  ];
  N['sanctum-stormreach'] = [
    npc(4, 10, 'e', 'trainer', 'Coil Adept Ryn',
      ['Ryn: Charged and ready.'],
      { trainer: T('Coil Adept Ryn', 'trainer', [p(23, 24), p(47, 25)], 1900,
        ['Ryn: Spark it!'], ['Ryn: Grounded me.'], ['Ryn: Hah!']) }),
    npc(11, 10, 'w', 'trainer', 'Coil Adept Sith',
      ['Sith: Volsa is just up there. Earn it.'],
      { trainer: T('Coil Adept Sith', 'trainer', [p(24, 25), p(12, 24), p(23, 25)], 1900,
        ['Sith: Here!'], ['Sith: Good.'], ['Sith: Ha!']) }),
    npc(7, 3, 's', 'warden', 'Warden Volsa',
      storm.warden.intro.concat([{ battle: storm.warden }], storm.post),
      { once: 'sigil4', warden: true }),
    npc(7, 3, 's', 'warden', 'Warden Volsa',
      ['Volsa: North for Gravemoor. Warden Mourn. Umbra.',
       'Volsa: Quiet sort. Hits like a landslide.'],
      { requireFlag: 'sigil4' })
  ];

  /* ===================== Route 5 / Gravemoor ===================== */
  N['route5'] = [
    sign(10, 28, 'ROUTE 5 — Gravemoor north.'),
    sign(14, 12, 'Cliff face. Climbable, with the right Sigil.'),
    npc(8, 8, 'e', 'trainer', 'Moorwalker Dain',
      ['Dain: Long way up. Rest here a moment — after this.'],
      { trainer: T('Moorwalker Dain', 'trainer', [p(39, 22), p(21, 23)], 1600,
        ['Dain: Come on.'], ['Dain: Go well.'], ['Dain: Ha!']) }),
    npc(18, 18, 'w', 'trainer', 'Seeker Ovi',
      ['Ovi: Spirits up here. And me.'],
      { trainer: T('Seeker Ovi', 'trainer', [p(26, 22), p(27, 23), p(39, 22)], 1650,
        ['Ovi: Listen.'], ['Ovi: Well heard.'], ['Ovi: Hah.']) })
  ];

  var grave = wardenScript('gravemoor');
  N['gravemoor'] = [
    sign(12, 12, 'GRAVEMOOR — keep a lamp lit. Umbra Sanctum east.'),
    sign(17, 20, 'The stones out on the moor are older than the town. Leave them be.'),
    npc(10, 13, 's', 'elder', 'Lampkeeper',
      ['Lampkeeper: We light them at dusk and we do not let them go out.',
       'Lampkeeper: You will understand why if you stay past dark.']),
    npc(23, 24, 'n', 'villager', 'Digger',
      ['Digger: The Hand hired half the town to dig at the Spire. I said no.',
       'Digger: Plenty said yes.'])
  ];
  N['hearth-gravemoor'] = hearthStaff('shop-gravemoor');
  N['grave-shop'] = [
    npc(6, 4, 's', 'clerk', 'Supplier',
      ['Supplier: Ethers now. PP runs out faster than you\'d think.'],
      { shop: 'shop-gravemoor' })
  ];
  N['grave-house'] = [
    npc(9, 4, 'w', 'elder', 'Historian',
      ['Historian: Spirit and Umbra hit each other hard, both ways.',
       'Historian: Cold iron bites a Spirit too. Old folklore, and quite true.'])
  ];
  N['sanctum-gravemoor'] = [
    npc(4, 10, 'e', 'trainer', 'Mourner Vail',
      ['Vail: Softly, now.'],
      { trainer: T('Mourner Vail', 'trainer', [p(39, 28), p(26, 29)], 2400,
        ['Vail: Quietly.'], ['Vail: Go on up.'], ['Vail: Quietly.']) }),
    npc(11, 10, 'w', 'trainer', 'Mourner Esk',
      ['Esk: The Warden waits above.'],
      { trainer: T('Mourner Esk', 'trainer', [p(52, 29), p(33, 29), p(39, 30)], 2400,
        ['Esk: Come.'], ['Esk: Well met.'], ['Esk: Hm.']) }),
    npc(7, 3, 's', 'warden', 'Warden Mourn',
      grave.warden.intro.concat([{ battle: grave.warden }], grave.post),
      { once: 'sigil5', warden: true }),
    npc(7, 3, 's', 'warden', 'Warden Mourn',
      ['Mourn: East to Ironhold. Gild will test your metal, and mean it literally.'],
      { requireFlag: 'sigil5' })
  ];

  /* ===================== Route 6 / Hollow Spire ===================== */
  N['route6'] = [
    sign(12, 11, 'ROUTE 6 — Ironhold east. Hollow Spire north.'),
    sign(26, 9, 'HOLLOW SPIRE — closed by order of the Ashen Hand.'),
    npc(30, 6, 's', 'ashen', 'Ashen Adept',
      [
        'Adept: The Spire is Hand ground now. Turn around.',
        { battle: T('Ashen Adept', 'ashen', [p(33, 26), p(39, 27), p(52, 26)], 2000,
          ['Adept: You were warned twice.'],
          ['Adept: ...Go and look, then. It changes nothing.'],
          ['Adept: Turn around.']) },
        { flag: 'ashen2', val: true },
        'Adept: Look all you want. The Hierarch is already inside.'
      ],
      { once: 'ashen2', blocksUntil: 'ashen2' }),
    npc(8, 6, 's', 'trainer', 'Ridge Guide Hal',
      ['Hal: You\'ll want a rest before the Spire.'],
      { trainer: T('Ridge Guide Hal', 'trainer', [p(37, 26), p(21, 27)], 2000,
        ['Hal: Up we go.'], ['Hal: Good climb.'], ['Hal: Ha!']) }),
    npc(24, 16, 'n', 'trainer', 'Collector Wenn',
      ['Wenn: Six of mine, four of yours. Fine, three.'],
      { trainer: T('Collector Wenn', 'trainer', [p(33, 26), p(34, 27), p(29, 27)], 2100,
        ['Wenn: Observe.'], ['Wenn: Noted.'], ['Wenn: As expected.']) })
  ];

  N['hollowspire'] = [
    npc(11, 24, 's', 'ashen', 'Ashen Adept',
      ['Adept: Deeper in. You won\'t like it.'],
      { trainer: T('Ashen Adept', 'ashen', [p(39, 28), p(33, 28), p(52, 29)], 2300,
        ['Adept: For the Hierarch.'], ['Adept: ...Go on, then.'], ['Adept: For the Hierarch.']) }),
    npc(6, 5, 's', 'ashen', 'Ashen Adept',
      ['Adept: The Root is not yours. It is not anyone\'s.'],
      { trainer: T('Ashen Adept', 'ashen', [p(52, 29), p(40, 29)], 2300,
        ['Adept: Nothing personal.'], ['Adept: ...Nothing personal.'], ['Adept: Nothing personal.']) }),
    /* Story beat: the Root Charm, gated behind the second Ashen encounter. */
    npc(17, 5, 's', 'prof', 'Professor Rowan',
      [
        'Rowan: I came as fast as I could. Look at this.',
        'Rowan: They have cut down to the World-Root itself.',
        'Rowan: This was on the floor. It is old, and it is warm.',
        { give: 'root-charm' },
        'Rowan: Keep it. If Verdurion stirs, that will know before we do.',
        'Rowan: Get your eighth Sigil. Then come back here.',
        { flag: 'gotRootCharm', val: true }
      ],
      { once: 'gotRootCharm' }),
    /* The finale, once all eight Sigils are in the case. */
    npc(11, 8, 's', 'ashen', 'Hierarch Vane',
      [
        'Vane: Eight Sigils. You have been busy.',
        'Vane: So have we. The Root is awake, tamer. It has been awake for an hour.',
        'Vane: It will remake Verdane, and I will be standing where it starts.',
        { battle: T('Hierarch Vane', 'ashen',
          [p(6, 48), p(44, 48), p(52, 49), p(33, 48), p(40, 50)], 12000,
          ['Vane: Then stand in front of it. See what that earns you.'],
          ['Vane: ...I was standing too close after all.'],
          ['Vane: As I said. Nothing you do matters.']) },
        { flag: 'vaneDefeated', val: true },
        'Vane: ...Go on. It is through there. It is awake.',
        'Vane: Whatever happens next, you asked for it as much as I did.',
        'Rowan: Then we do not run. We meet it.',
        { give: 'truestone' },
        'Rowan: One Truestone. It has never failed. Do not waste it.'
      ],
      { once: 'vaneDefeated', requireBadges: 8 }),
    npc(11, 4, 's', 'villager', 'Verdurion',
      [
        'The World-Root shifts. Something enormous turns over beneath the stone.',
        'Verdurion rises.',
        { wild: { sp: 56, lvl: 50 } },
        { flag: 'verdurionDone', val: true },
        'The Spire goes quiet. Whatever was decided here, it is decided.'
      ],
      { once: 'verdurionDone', requireFlag: 'vaneDefeated' })
  ];

  /* ===================== Ironhold ===================== */
  var iron = wardenScript('ironhold');
  N['ironhold'] = [
    sign(12, 14, 'IRONHOLD — forge town. Iron Sanctum southeast.'),
    sign(17, 7, 'Mind the slag. It stays hot for a week.'),
    npc(11, 15, 's', 'miner', 'Forgehand',
      ['Forgehand: Gild tests everything before it leaves the town. Everything.']),
    npc(24, 5, 'w', 'villager2', 'Quartermaster',
      ['Quartermaster: Evolution shards in the shop now. Rare stock, rare price.'])
  ];
  N['hearth-ironhold'] = hearthStaff('shop-ironhold');
  N['iron-shop'] = [
    npc(6, 4, 's', 'clerk', 'Supplier',
      ['Supplier: Shards, Voidstones, the good salves. Ironhold gets the best of it.'],
      { shop: 'shop-ironhold' })
  ];
  N['iron-house'] = [
    npc(4, 4, 'e', 'elder', 'Retired Smith',
      ['Retired Smith: Some aetherlings only change when they touch the right shard.',
       'Retired Smith: Use one from the bag. You\'ll know if it takes.'])
  ];
  N['sanctum-ironhold'] = [
    npc(4, 10, 'e', 'trainer', 'Forgeguard Ott',
      ['Ott: Tested and passed, or tested and not.'],
      { trainer: T('Forgeguard Ott', 'trainer', [p(37, 32), p(22, 33)], 3000,
        ['Ott: Begin.'], ['Ott: Passed.'], ['Ott: Failed.']) }),
    npc(11, 10, 'w', 'trainer', 'Forgeguard Wex',
      ['Wex: Second test.'],
      { trainer: T('Forgeguard Wex', 'trainer', [p(38, 33), p(46, 33), p(37, 34)], 3000,
        ['Wex: Begin.'], ['Wex: Passed.'], ['Wex: Failed.']) }),
    npc(7, 3, 's', 'warden', 'Warden Gild',
      iron.warden.intro.concat([{ battle: iron.warden }], iron.post),
      { once: 'sigil6', warden: true }),
    npc(7, 3, 's', 'warden', 'Warden Gild',
      ['Gild: North to Frostvale. Bryne keeps the coldest Sanctum in Verdane.',
       'Gild: Bring something that likes the cold.'],
      { requireFlag: 'sigil6' })
  ];

  /* ===================== Route 7 / Ashen hideout ===================== */
  N['route7'] = [
    sign(14, 18, 'ROUTE 7 — Frostvale north. Getting colder.'),
    sign(10, 30, 'Unmarked door in the rock face to the west. Recently used.'),
    npc(8, 20, 'e', 'trainer', 'Frost Scout Rell',
      ['Rell: Cold enough to talk fast. Let\'s go.'],
      { trainer: T('Frost Scout Rell', 'trainer', [p(39, 30), p(37, 31)], 2600,
        ['Rell: Quickly!'], ['Rell: Well fought.'], ['Rell: Ha!']) }),
    npc(15, 22, 'w', 'trainer', 'Pathfinder Ana',
      ['Ana: Nearly at the snowline. Warm up on me.'],
      { trainer: T('Pathfinder Ana', 'trainer', [p(49, 31), p(40, 31), p(21, 32)], 2700,
        ['Ana: Here!'], ['Ana: Good.'], ['Ana: Hah!']) })
  ];

  N['ashen-hideout'] = [
    npc(10, 12, 's', 'ashen', 'Ashen Adept',
      ['Adept: Wrong door, tamer.'],
      { trainer: T('Ashen Adept', 'ashen', [p(33, 32), p(40, 32)], 2800,
        ['Adept: Out.'], ['Adept: ...Out.'], ['Adept: Out.']) }),
    npc(5, 7, 's', 'ashen', 'Ashen Adept',
      ['Adept: Nothing here for you.'],
      { trainer: T('Ashen Adept', 'ashen', [p(52, 33), p(29, 32), p(39, 33)], 2900,
        ['Adept: Leave.'], ['Adept: ...Leave.'], ['Adept: Leave.']) }),
    npc(16, 7, 's', 'ashen', 'Adept Coran',
      [
        'Coran: You are not supposed to be able to find this place.',
        { battle: T('Adept Coran', 'ashen', [p(6, 34), p(44, 34), p(33, 35)], 4000,
          ['Coran: Then I deal with it myself.'],
          ['Coran: ...The Hierarch is at the Spire. Go and be disappointed.'],
          ['Coran: Dealt with.']) },
        { flag: 'ashen3', val: true },
        { give: 'ashen-key' },
        'Coran: Take the key. It opens nothing you\'ll like.',
        { give: 'gleam-shard' }
      ],
      { once: 'ashen3' }),
    npc(10, 17, 'n', 'villager', 'Freed Digger',
      ['Digger: They had me down at the Spire for a fortnight.',
       'Digger: Whatever is under there, it moved while I was working.'])
  ];

  /* ===================== Frostvale ===================== */
  var frost = wardenScript('frostvale');
  N['frostvale'] = [
    sign(12, 12, 'FROSTVALE — the wall was built against something. Sanctum east.'),
    sign(17, 20, 'Deep snow hides deep holes. Walk the cleared road.'),
    npc(10, 13, 's', 'villager', 'Wallwatch',
      ['Wallwatch: Glaciark came through the old wall in my grandmother\'s day.',
       'Wallwatch: We built the new one further back. Seemed wiser.']),
    npc(23, 24, 'n', 'villager2', 'Trader',
      ['Trader: Two Sigils left. Then Aurel Citadel, and the Conclave.'])
  ];
  N['hearth-frostvale'] = hearthStaff('shop-frostvale');
  N['frost-shop'] = [
    npc(6, 4, 's', 'clerk', 'Supplier',
      ['Supplier: Thaw Oil. Trust me, you will want it here.'],
      { shop: 'shop-frostvale' })
  ];
  N['frost-house'] = [
    npc(9, 4, 'w', 'elder', 'Icewright',
      ['Icewright: A frozen aetherling cannot move at all until it thaws.',
       'Icewright: An Ember move will thaw it. So will luck, eventually.'])
  ];
  N['sanctum-frostvale'] = [
    npc(4, 10, 'e', 'trainer', 'Rimeguard Sil',
      ['Sil: Cold start.'],
      { trainer: T('Rimeguard Sil', 'trainer', [p(41, 36), p(45, 37)], 3600,
        ['Sil: Begin.'], ['Sil: Warm work.'], ['Sil: Cold.']) }),
    npc(11, 10, 'w', 'trainer', 'Rimeguard Toft',
      ['Toft: Bryne is above. Mind the floor.'],
      { trainer: T('Rimeguard Toft', 'trainer', [p(48, 37), p(46, 37), p(42, 38)], 3600,
        ['Toft: Begin.'], ['Toft: Well held.'], ['Toft: Cold.']) }),
    npc(7, 3, 's', 'warden', 'Warden Bryne',
      frost.warden.intro.concat([{ battle: frost.warden }], frost.post),
      { once: 'sigil7', warden: true }),
    npc(7, 3, 's', 'warden', 'Warden Bryne',
      ['Bryne: East to Skyhaven. Kestrel is the last Warden.',
       'Bryne: After that there is only Aurel, and they do not go easy either.'],
      { requireFlag: 'sigil7' })
  ];

  /* ===================== Route 8 / Skyhaven ===================== */
  N['route8'] = [
    sign(12, 11, 'ROUTE 8 — Skyhaven east. High and cold.'),
    sign(30, 9, 'Rockfall. Clear it or climb around.'),
    npc(10, 6, 's', 'trainer', 'Alpinist Grey',
      ['Grey: Thin air up here. Short battle, then.'],
      { trainer: T('Alpinist Grey', 'trainer', [p(45, 35), p(42, 36)], 3200,
        ['Grey: Up!'], ['Grey: Good lungs.'], ['Grey: Hah!']) }),
    npc(24, 16, 'n', 'trainer', 'Skywatch Nim',
      ['Nim: Watching for Aetherwyrm. Seen one twice.'],
      { trainer: T('Skywatch Nim', 'trainer', [p(49, 36), p(54, 36), p(48, 36)], 3400,
        ['Nim: Look up.'], ['Nim: Well spotted.'], ['Nim: Ha!']) })
  ];

  var sky = wardenScript('skyhaven');
  N['skyhaven'] = [
    sign(12, 10, 'SKYHAVEN — last Sanctum before the Citadel.'),
    sign(17, 19, 'Route 9 north. Conclave challengers only past the gate.'),
    npc(10, 11, 's', 'villager', 'Gatekeeper',
      ['Gatekeeper: Eight Sigils gets you up Route 9. Seven gets you a nice view.']),
    npc(23, 23, 'n', 'villager2', 'Old Challenger',
      ['Old Challenger: I got as far as the third Master. Twice.',
       'Old Challenger: Bring more than four. Bring six, and bring revives.'])
  ];
  N['hearth-skyhaven'] = hearthStaff('shop-skyhaven');
  N['sky-shop'] = [
    npc(6, 4, 's', 'clerk', 'Supplier',
      ['Supplier: Last shop before the Citadel. Stock up properly.'],
      { shop: 'shop-skyhaven' })
  ];
  N['sky-house'] = [
    npc(4, 4, 'e', 'elder', 'Retired Master',
      ['Retired Master: The Conclave is five battles with no rest between them.',
       'Retired Master: Whatever you bring in is what you finish with.'])
  ];
  N['sanctum-skyhaven'] = [
    npc(4, 10, 'e', 'trainer', 'Galehand Pell',
      ['Pell: Windy today. Always is.'],
      { trainer: T('Galehand Pell', 'trainer', [p(13, 40), p(49, 41)], 4200,
        ['Pell: Go!'], ['Pell: Clean.'], ['Pell: Hah!']) }),
    npc(11, 10, 'w', 'trainer', 'Galehand Ros',
      ['Ros: Kestrel is last. Then the Citadel.'],
      { trainer: T('Galehand Ros', 'trainer', [p(31, 41), p(50, 41), p(54, 42)], 4200,
        ['Ros: Go!'], ['Ros: Well flown.'], ['Ros: Hah!']) }),
    npc(7, 3, 's', 'warden', 'Warden Kestrel',
      sky.warden.intro.concat([{ battle: sky.warden }], sky.post),
      { once: 'sigil8', warden: true }),
    npc(7, 3, 's', 'warden', 'Warden Kestrel',
      ['Kestrel: The Spire, then the Citadel. In that order, if you have sense.'],
      { requireFlag: 'sigil8' })
  ];

  /* ===================== Route 9 / Aurel Citadel ===================== */
  N['route9'] = [
    sign(14, 28, 'ROUTE 9 — Aurel Citadel north. Challengers only.'),
    sign(10, 12, 'The last stretch. Everything up here is old and strong.'),
    npc(12, 26, 's', 'rival', 'Ferren',
      [
        'Ferren: Thought I\'d beat you here. I did, actually.',
        'Ferren: But I\'m not going in until I\'ve had this.',
        { battle: rival(4) },
        { flag: 'beatRival5', val: true },
        'Ferren: ...All right. All right.',
        'Ferren: Go and win it. I\'ll be right behind you, and then I\'ll take it off you.'
      ],
      { once: 'beatRival5', blocksUntil: 'beatRival5' }),
    npc(8, 8, 'e', 'trainer', 'Aspirant Doro',
      ['Doro: Sixth attempt. Still climbing.'],
      { trainer: T('Aspirant Doro', 'trainer', [p(51, 40), p(47, 41), p(42, 41)], 4600,
        ['Doro: Again!'], ['Doro: ...Again next year.'], ['Doro: Ha!']) }),
    npc(18, 18, 'w', 'trainer', 'Aspirant Kesh',
      ['Kesh: Warm-up for the Conclave. For you, I mean.'],
      { trainer: T('Aspirant Kesh', 'trainer', [p(54, 41), p(49, 42), p(52, 42), p(46, 41)], 4800,
        ['Kesh: Show me.'], ['Kesh: Shown.'], ['Kesh: Ha!']) })
  ];

  N['aurel'] = [
    sign(12, 12, 'AUREL CITADEL — seat of the Conclave.'),
    sign(17, 20, 'Eight Sigils required beyond the north gate. No exceptions.'),
    npc(10, 13, 's', 'conclave', 'Steward',
      ['Steward: Eight Sigils, and the Conclave will see you.',
       'Steward: Four Masters, then the Champion. No rest between them.']),
    npc(23, 22, 'n', 'villager', 'Citadel Cook',
      ['Cook: Everyone comes out of there hungry. Win or lose.'])
  ];
  N['hearth-aurel'] = hearthStaff('shop-aurel');
  N['aurel-shop'] = [
    npc(6, 4, 's', 'clerk', 'Supplier',
      ['Supplier: Last chance. Buy more revives than you think you need.'],
      { shop: 'shop-aurel' })
  ];

  /* The Conclave: four Masters and the Champion, bottom to top. */
  N['conclave'] = [
    npc(9, 26, 's', 'conclave', 'Master Sable',
      [
        'Sable: First of four. Nothing personal in any of it.',
        { battle: T('Master Sable', 'conclave',
          [p(52, 48), p(40, 48), p(33, 49), p(27, 49), p(44, 50)], 10000,
          ['Sable: Begin.'],
          ['Sable: Clean. Go up.'],
          ['Sable: That is where most people stop.']) },
        { flag: 'conclave1', val: true },
        'Sable: Up you go. Corrin is next, and Corrin does not move.'
      ],
      { once: 'conclave1', blocksUntil: 'conclave1' }),
    npc(9, 20, 's', 'conclave', 'Master Corrin',
      [
        'Corrin: Second. I am the wall, not the door.',
        { battle: T('Master Corrin', 'conclave',
          [p(22, 49), p(51, 49), p(38, 50), p(36, 50), p(46, 51)], 10000,
          ['Corrin: Come through, then.'],
          ['Corrin: ...You came through.'],
          ['Corrin: The wall holds.']) },
        { flag: 'conclave2', val: true },
        'Corrin: Go on. Ivane is colder than she looks, and she looks cold.'
      ],
      { once: 'conclave2', blocksUntil: 'conclave2', requireFlag: 'conclave1' }),
    npc(9, 14, 's', 'conclave', 'Master Ivane',
      [
        'Ivane: Third. You must be tired. That is rather the point.',
        { battle: T('Master Ivane', 'conclave',
          [p(46, 50), p(44, 50), p(48, 50), p(29, 51), p(42, 52)], 11000,
          ['Ivane: Let us be quick.'],
          ['Ivane: Quick indeed. Go up.'],
          ['Ivane: Rest. Genuinely — rest.']) },
        { flag: 'conclave3', val: true },
        'Ivane: Thal is last of us. He will try to end it in two turns.'
      ],
      { once: 'conclave3', blocksUntil: 'conclave3', requireFlag: 'conclave2' }),
    npc(9, 8, 's', 'conclave', 'Master Thal',
      [
        'Thal: Fourth and last. Then the Champion, and she is worse than me.',
        { battle: T('Master Thal', 'conclave',
          [p(31, 51), p(36, 51), p(6, 52), p(25, 52), p(55, 53)], 12000,
          ['Thal: Fast, now.'],
          ['Thal: ...Fast enough. Go.'],
          ['Thal: Too slow.']) },
        { flag: 'conclave4', val: true },
        'Thal: One door left. Good luck. You will need a quantity of it.'
      ],
      { once: 'conclave4', blocksUntil: 'conclave4', requireFlag: 'conclave3' }),
    npc(9, 2, 's', 'conclave', 'Champion Auria',
      [
        'Auria: Four Masters and you are still standing. Good.',
        'Auria: I have held this seat for eleven years. I would like to keep it.',
        'Auria: Show me why I shouldn\'t.',
        { battle: T('Champion Auria', 'conclave',
          [p(3, 54), p(9, 54), p(25, 55), p(46, 55), p(52, 55), p(55, 57)], 20000,
          ['Auria: Everything you have. Now.'],
          ['Auria: ...Eleven years. Well held, tamer.'],
          ['Auria: Not this time. Come back — I mean that.']) },
        { flag: 'champion', val: true },
        'Auria: The seat is yours. Verdane has a new Champion.',
        'Rowan: I came up to watch. I am glad I did.',
        'Rowan: Go home for a day. Then go and finish the dex.',
        { flag: 'gameComplete', val: true }
      ],
      { once: 'champion', requireFlag: 'conclave4' })
  ];

  /* Field-skill obstacle prompts, keyed by tile character. */
  AE.OBSTACLES = {
    'B': { skill: 'cleave', text: 'Thick brush blocks the way.' },
    'R': { skill: 'shatter', text: 'A cracked boulder blocks the way.' },
    'C': { skill: 'ascend', text: 'A sheer cliff rises here.' },
    '~': { skill: 'surge', text: 'The water is deep and open.' }
  };

  /* Gates that need Sigils before the road opens. */
  AE.GATES = [
    { map: 'route9', x: 12, y: 32, badges: 8,
      text: 'A Citadel steward blocks the road. "Eight Sigils, or turn back."' }
  ];

})(window.AE = window.AE || {});
