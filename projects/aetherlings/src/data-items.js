/* Aetherlings — items, bag pockets and shop stock. */
(function (AE) {
  'use strict';

  function it(id, name, cat, price, use, desc) {
    return { id: id, name: name, cat: cat, price: price, use: use || {}, desc: desc };
  }

  var LIST = [
    /* --- Capture stones (pocket: stones) ------------------------------- */
    it('bindstone', 'Bindstone', 'stones', 200, { catch: 1 },
       'A carved stone that binds a weakened aetherling.'),
    it('runestone', 'Runestone', 'stones', 600, { catch: 1.5 },
       'A better-cut binding stone.'),
    it('voidstone', 'Voidstone', 'stones', 1200, { catch: 2 },
       'Binding stone of Conclave make.'),
    it('truestone', 'Truestone', 'stones', 0, { catch: 255 },
       'It has never once failed. There is only the one.'),

    /* --- Healing (pocket: heal) ---------------------------------------- */
    it('salve', 'Salve', 'heal', 300, { heal: 20 }, 'Restores 20 HP.'),
    it('greater-salve', 'Greater Salve', 'heal', 700, { heal: 50 }, 'Restores 50 HP.'),
    it('master-salve', 'Master Salve', 'heal', 1500, { heal: 200 }, 'Restores 200 HP.'),
    it('full-mend', 'Full Mend', 'heal', 2500, { healAll: true, cure: 'all' },
       'Fully restores HP and cures any condition.'),
    it('revive', 'Revive', 'heal', 1500, { revive: 0.5 }, 'Revives a fainted aetherling to half HP.'),
    it('greater-revive', 'Greater Revive', 'heal', 4000, { revive: 1 }, 'Revives to full HP.'),
    it('antidote', 'Antidote', 'heal', 100, { cure: 'poison' }, 'Cures poisoning.'),
    it('burn-balm', 'Burn Balm', 'heal', 250, { cure: 'burn' }, 'Cures a burn.'),
    it('thaw-oil', 'Thaw Oil', 'heal', 250, { cure: 'freeze' }, 'Thaws a frozen aetherling.'),
    it('wake-root', 'Wake Root', 'heal', 250, { cure: 'sleep' }, 'Rouses a sleeping aetherling.'),
    it('static-cloth', 'Static Cloth', 'heal', 200, { cure: 'para' }, 'Cures paralysis.'),
    it('cure-all', 'Cure-All', 'heal', 600, { cure: 'all' }, 'Cures any status condition.'),
    it('ether', 'Ether', 'heal', 1200, { pp: 10 }, 'Restores 10 PP to one move.'),
    it('elixir', 'Elixir', 'heal', 2000, { pp: 'all' }, 'Restores PP to every move.'),

    /* --- Battle-only charms (pocket: battle) --------------------------- */
    it('power-charm', 'Power Charm', 'battle', 500, { stat: { atk: 1 }, battleOnly: true },
       'Raises Attack for one battle.'),
    it('guard-charm', 'Guard Charm', 'battle', 500, { stat: { def: 1 }, battleOnly: true },
       'Raises Defence for one battle.'),
    it('focus-charm', 'Focus Charm', 'battle', 500, { stat: { spa: 1 }, battleOnly: true },
       'Raises Sp. Atk for one battle.'),
    it('swift-charm', 'Swift Charm', 'battle', 350, { stat: { spe: 1 }, battleOnly: true },
       'Raises Speed for one battle.'),
    it('flee-charm', 'Flee Charm', 'battle', 300, { flee: true, battleOnly: true },
       'Guarantees escape from a wild aetherling.'),

    /* --- Evolution shards (pocket: stones) ----------------------------- */
    it('verdant-shard', 'Verdant Shard', 'stones', 3000, { evoStone: true },
       'A green shard. Some aetherlings change when they hold it.'),
    it('storm-shard', 'Storm Shard', 'stones', 3000, { evoStone: true },
       'A shard that hums before a storm.'),

    /* --- Valuables ------------------------------------------------------ */
    it('gleam-shard', 'Gleam Shard', 'heal', 0, { sell: 1000 },
       'Worth a good deal to the right buyer.'),

    /* --- Key items (pocket: key) ---------------------------------------- */
    it('tamer-card', 'Tamer Card', 'key', 0, { key: true }, 'Proof you are a registered tamer.'),
    it('sigil-case', 'Sigil Case', 'key', 0, { key: true }, 'Holds the Sanctum Sigils you have earned.'),
    it('world-map', 'World Map', 'key', 0, { key: true }, 'A folded map of the Verdane region.'),
    it('ashen-key', 'Ashen Key', 'key', 0, { key: true }, 'Taken from an Ashen Hand adept. It opens something.'),
    it('root-charm', 'Root Charm', 'key', 0, { key: true }, 'Warm to the touch. It points, faintly, downward.')
  ];

  AE.ITEMS = {};
  LIST.forEach(function (i) { AE.ITEMS[i.id] = i; });
  AE.ITEM_LIST = LIST;

  AE.item = function (id) {
    var i = AE.ITEMS[id];
    if (!i) throw new Error('Unknown item: ' + id);
    return i;
  };

  AE.POCKETS = [
    { id: 'heal', name: 'Remedies' },
    { id: 'stones', name: 'Stones' },
    { id: 'battle', name: 'Charms' },
    { id: 'key', name: 'Key Items' }
  ];

  /* Shop stock, keyed by the map id of the shop interior. Later towns stock more. */
  AE.SHOPS = {
    'shop-willowmere':  ['bindstone', 'salve', 'antidote'],
    'shop-thornhollow': ['bindstone', 'salve', 'antidote', 'static-cloth', 'burn-balm'],
    'shop-cinderfall':  ['bindstone', 'runestone', 'salve', 'greater-salve', 'antidote', 'burn-balm', 'static-cloth', 'flee-charm'],
    'shop-brackwater':  ['bindstone', 'runestone', 'greater-salve', 'cure-all', 'revive', 'swift-charm', 'flee-charm'],
    'shop-stormreach':  ['runestone', 'greater-salve', 'cure-all', 'revive', 'power-charm', 'guard-charm', 'swift-charm'],
    'shop-gravemoor':   ['runestone', 'greater-salve', 'cure-all', 'revive', 'ether', 'focus-charm', 'power-charm'],
    'shop-ironhold':    ['runestone', 'voidstone', 'master-salve', 'cure-all', 'revive', 'ether', 'guard-charm', 'storm-shard', 'verdant-shard'],
    'shop-frostvale':   ['voidstone', 'master-salve', 'cure-all', 'revive', 'ether', 'thaw-oil', 'focus-charm'],
    'shop-skyhaven':    ['voidstone', 'master-salve', 'full-mend', 'revive', 'greater-revive', 'elixir', 'power-charm', 'focus-charm'],
    'shop-aurel':       ['voidstone', 'full-mend', 'greater-revive', 'elixir', 'power-charm', 'guard-charm', 'focus-charm', 'swift-charm']
  };

})(window.AE = window.AE || {});
