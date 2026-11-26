/*
 * Per-input overrides fall back to the Configuration page.
 *
 * The ADS-B host, port and radius fields on an input are overrides: blank
 * means "use the Configuration page". They used to be pre-filled, so every
 * input saved its own copy and the Configuration page never applied, and a
 * disabled input called "default" shipped in default/inputs.conf and showed in
 * the Inputs table.
 *
 * What the collector used is read from the poll line it logs to
 * ta_airspace_watch.log (index=_internal), and stanzas from splunkd's raw
 * configs/conf-inputs endpoint; neither is a route the page or the add-on's
 * REST handler uses. Labels are written out, not looked up in globalConfig.json.
 */
const path = require('path');
const { test, expect } = require('@playwright/test');
const ucc = require('./ucc/ucc-helpers');

const GC = require(path.join(__dirname, '..', 'globalConfig.json'));
const APP = GC.meta.name;
const SETTINGS_CONF = `${GC.meta.restRoot.toLowerCase()}_settings`;
const PAGE_TITLE = GC.pages.inputs.title;
const SERVICE_TITLE = 'ADS-B Receiver';
const L = { name: 'Name', interval: 'Interval (s)', host: 'ADS-B host (override)', port: 'ADS-B port (override)',
    radius: 'Radius (nm, override)' };
const OVERRIDE_KEYS = ['adsb_host', 'adsb_port', 'radius_nm'];

// Configuration page values for this spec: the compose network's mock
// receiver, and a radius no default or override uses.
const CONFIG = { receiver: { adsb_host: 'mock-receiver', adsb_port: '8080' }, geofence: { radius_nm: '77' } };

let rest;
const saved = {};
test.beforeAll(async () => {
    rest = await ucc.restClient();
    for (const [stanza, values] of Object.entries(CONFIG)) {
        saved[stanza] = await rest.confStanza(APP, SETTINGS_CONF, stanza);
        await rest.setConf(APP, SETTINGS_CONF, stanza, values);
    }
});
test.afterAll(async () => {
    for (const [stanza, values] of Object.entries(CONFIG)) {
        const back = Object.fromEntries(Object.keys(values).map((k) => [k, (saved[stanza] || {})[k] || '']));
        await rest.setConf(APP, SETTINGS_CONF, stanza, back);
    }
    await rest.dispose();
});
test.beforeEach(async ({ page }) => ucc.login(page));

const blank = (v) => v === undefined || v === null || v === '';

/** The newest poll line for one input logged after `since` (epoch s), parsed.
 *  Search results come newest first. */
async function latestPoll(name, since) {
    const rows = await rest.search(
        `index=_internal source=*ta_airspace_watch.log* "[airspace_watch://${name}] poll" | table _raw`, since);
    if (!rows.length) return null;
    const m = /host=(\S+):(\d+) path=\S+ radius_nm=(\S+)/.exec(rows[0]._raw) || [];
    return { host: m[1], port: m[2], radius: m[3] && Number(m[3]) };
}

/** Wait until the newest poll line after `since` shows `want`. A run that
 *  started just before an edit can still log the old value once, so this
 *  waits for the collector to settle rather than reading the first line. */
async function expectPoll(name, since, want, what) {
    let last = null;
    await expect.poll(async () => {
        last = await latestPoll(name, since);
        return last && Object.entries(want).every(([k, v]) => last[k] === v);
    }, { message: `${what}; newest poll line: ${JSON.stringify(last)}`, timeout: 180000, intervals: [10000] })
        .toBe(true);
}

// A second back: splunkd refuses (400) a search whose earliest time is the
// current second, and expectPoll waits for the newest line anyway.
const now = () => Math.floor(Date.now() / 1000) - 1;

test('no shipped input appears on the Inputs page', async ({ page }) => {
    expect(await rest.inputStanza(APP, 'airspace_watch', 'default'), '[airspace_watch://default] still ships').toBeNull();
    await ucc.gotoUccPage(page, APP, 'inputs', PAGE_TITLE);
    await expect(page.getByRole('button', { name: 'Create New Input' })).toBeVisible();
    await page.waitForTimeout(2000);
    await expect(ucc.inputRow(page, 'default')).toHaveCount(0);
});

test('a new input starts with its overrides empty', async ({ page }) => {
    await ucc.gotoUccPage(page, APP, 'inputs', PAGE_TITLE);
    const d = await ucc.openCreateInput(page, SERVICE_TITLE);
    for (const key of ['host', 'port', 'radius']) {
        expect(await ucc.readText(d, L[key]), `${L[key]} is pre-filled`).toBe('');
    }
    await d.getByRole('button', { name: 'Cancel', exact: true }).click();
});

test('blank overrides use the Configuration page; a set override wins; clearing it falls back', async ({ page }) => {
    const name = `e2e_fallback_${Date.now().toString(36)}`;
    try {
        await ucc.gotoUccPage(page, APP, 'inputs', PAGE_TITLE);
        const d = await ucc.openCreateInput(page, SERVICE_TITLE);
        await ucc.fillForm(d, { [L.name]: name, [L.interval]: '10', [L.host]: '', [L.port]: '', [L.radius]: '' });
        let since = now();
        await ucc.submitDialog(page, 'Add');
        const made = await ucc.expectConf(() => rest.inputStanza(APP, 'airspace_watch', name), (c) => c !== null,
            `[airspace_watch://${name}] created`);
        for (const k of OVERRIDE_KEYS) expect(blank(made[k]), `saved ${k}=${made[k]}`).toBe(true);
        await expectPoll(name, since, { host: 'mock-receiver', port: '8080', radius: 77 },
            'blank overrides: the Configuration page host, port and radius');

        // An override set on the input wins.
        await ucc.rowAction(page, name, 'Edit');
        await ucc.fillForm(ucc.dialog(page), { [L.radius]: '33' });
        await ucc.submitDialog(page, 'Update');
        await ucc.expectConf(() => rest.inputStanza(APP, 'airspace_watch', name), (c) => c && c.radius_nm === '33',
            'radius override saved');
        since = now();
        await expectPoll(name, since, { radius: 33 }, 'the radius override 33 is used');

        // Cleared again: back to the Configuration page.
        await ucc.rowAction(page, name, 'Edit');
        await ucc.fillForm(ucc.dialog(page), { [L.radius]: '' });
        await ucc.submitDialog(page, 'Update');
        await ucc.expectConf(() => rest.inputStanza(APP, 'airspace_watch', name), (c) => c && blank(c.radius_nm),
            'radius override cleared');
        since = now();
        await expectPoll(name, since, { radius: 77, host: 'mock-receiver' },
            'a cleared override falls back to the Configuration radius');
    } finally {
        await rest.deleteInput(APP, 'airspace_watch', name);
    }
});
