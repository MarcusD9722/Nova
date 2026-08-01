/* Aetherlings — elemental types and the effectiveness chart.
   12 types. Every type has at least one super-effective matchup and at least one
   weakness; ?test=1 asserts that so a bad edit can't silently create a dead type. */
(function (AE) {
  'use strict';

  AE.TYPES = [
    'Beast', 'Ember', 'Tide', 'Verdant', 'Storm', 'Frost',
    'Stone', 'Gale', 'Toxin', 'Iron', 'Spirit', 'Umbra'
  ];

  AE.TYPE_COLOR = {
    Beast: '#b9a882', Ember: '#f0703c', Tide: '#4a9ce8', Verdant: '#57c257',
    Storm: '#f0c838', Frost: '#7ed6e8', Stone: '#b8925c', Gale: '#9cb8f0',
    Toxin: '#b45cc4', Iron: '#a6aeba', Spirit: '#e07ab8', Umbra: '#6c5f8c'
  };

  /* attacker -> { strong: 2x, weak: 0.5x, none: 0x }. Anything unlisted is 1x. */
  var CHART = {
    Beast:   { strong: ['Stone', 'Iron', 'Frost', 'Umbra'],   weak: ['Gale', 'Toxin', 'Verdant'],            none: ['Spirit'] },
    Ember:   { strong: ['Verdant', 'Frost', 'Iron', 'Toxin'], weak: ['Ember', 'Tide', 'Stone'],              none: [] },
    Tide:    { strong: ['Ember', 'Stone'],                    weak: ['Tide', 'Verdant', 'Storm'],            none: [] },
    Verdant: { strong: ['Tide', 'Stone'],                     weak: ['Ember', 'Verdant', 'Toxin', 'Gale', 'Iron'], none: [] },
    Storm:   { strong: ['Tide', 'Gale'],                      weak: ['Verdant', 'Storm'],                    none: ['Stone'] },
    Frost:   { strong: ['Verdant', 'Gale', 'Stone', 'Beast'], weak: ['Ember', 'Tide', 'Frost', 'Iron'],      none: [] },
    Stone:   { strong: ['Ember', 'Storm', 'Frost', 'Toxin'],  weak: ['Verdant', 'Tide', 'Gale'],             none: [] },
    Gale:    { strong: ['Verdant', 'Beast', 'Toxin'],         weak: ['Storm', 'Stone', 'Iron'],              none: [] },
    Toxin:   { strong: ['Verdant', 'Beast'],                  weak: ['Toxin', 'Stone', 'Spirit'],            none: ['Iron'] },
    Iron:    { strong: ['Frost', 'Stone', 'Gale', 'Spirit'],  weak: ['Ember', 'Tide', 'Storm', 'Iron'],      none: [] },
    Spirit:  { strong: ['Toxin', 'Beast', 'Umbra'],           weak: ['Spirit'],                              none: [] },
    Umbra:   { strong: ['Spirit', 'Verdant'],                 weak: ['Beast', 'Umbra', 'Iron'],              none: [] }
  };

  /* Flattened into a dense lookup once at load: EFF[attacker][defender] -> multiplier. */
  var EFF = {};
  AE.TYPES.forEach(function (atk) {
    EFF[atk] = {};
    AE.TYPES.forEach(function (def) { EFF[atk][def] = 1; });
    var row = CHART[atk];
    row.strong.forEach(function (d) { EFF[atk][d] = 2; });
    row.weak.forEach(function (d) { EFF[atk][d] = 0.5; });
    row.none.forEach(function (d) { EFF[atk][d] = 0; });
  });
  AE.EFF = EFF;

  /* Multiplier of one attacking type against a defender's (one or two) types. */
  AE.effectiveness = function (atkType, defTypes) {
    var m = 1;
    for (var i = 0; i < defTypes.length; i++) m *= EFF[atkType][defTypes[i]];
    return m;
  };

  AE.effectivenessText = function (m) {
    if (m === 0) return "It has no effect...";
    if (m >= 2) return "It's super effective!";
    if (m > 0 && m < 1) return "It's not very effective...";
    return '';
  };

  /* --- Growth curves: total EXP required to reach a given level (1..100). --- */
  AE.GROWTH = {
    fast: function (n) { return Math.floor(4 * n * n * n / 5); },
    medium: function (n) { return n * n * n; },
    slow: function (n) { return Math.floor(5 * n * n * n / 4); }
  };

  AE.expForLevel = function (growth, level) {
    if (level <= 1) return 0;
    return (AE.GROWTH[growth] || AE.GROWTH.medium)(level);
  };

  /* --- Natures: a small +10%/-10% stat tilt, purely for flavour and variety. --- */
  AE.NATURES = [
    { name: 'Hardy',   up: null,  down: null },
    { name: 'Bold',    up: 'def', down: 'atk' },
    { name: 'Brave',   up: 'atk', down: 'spe' },
    { name: 'Calm',    up: 'spd', down: 'atk' },
    { name: 'Modest',  up: 'spa', down: 'atk' },
    { name: 'Jolly',   up: 'spe', down: 'spa' },
    { name: 'Adamant', up: 'atk', down: 'spa' },
    { name: 'Timid',   up: 'spe', down: 'atk' },
    { name: 'Impish',  up: 'def', down: 'spa' },
    { name: 'Quiet',   up: 'spa', down: 'spe' }
  ];

  AE.natureMod = function (nature, stat) {
    var n = AE.NATURES.find(function (x) { return x.name === nature; });
    if (!n) return 1;
    if (n.up === stat) return 1.1;
    if (n.down === stat) return 0.9;
    return 1;
  };

  /* --- Status conditions --- */
  AE.STATUS = {
    none: { name: '', tag: '' },
    burn: { name: 'burned', tag: 'BRN' },
    poison: { name: 'poisoned', tag: 'PSN' },
    para: { name: 'paralysed', tag: 'PAR' },
    sleep: { name: 'asleep', tag: 'SLP' },
    freeze: { name: 'frozen', tag: 'FRZ' }
  };

})(window.AE = window.AE || {});
