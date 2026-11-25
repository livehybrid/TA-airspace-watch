/*
 * Configuration page of TA-airspace-watch, driven in a browser on a real
 * Splunk. Each save is read back from splunkd's raw configs/conf-* endpoint,
 * which neither the page nor the add-on's REST handler reads.
 *
 * The last test follows a saved value all the way to the collector: it reads
 * the poll line the modular input itself logs, so a setting that is saved but
 * never used fails here even though the page and the conf file look right.
 *
 * This TA has no account tab and no encrypted field, so there is no secret to
 * check; ucc/ucc-helpers.js has expectSecretNotEchoed() for TAs that do.
 */
const path = require('path');
const { test, expect } = require('@playwright/test');
const ucc = require('./ucc/ucc-helpers');

const GC = require(path.join(__dirname, '..', 'globalConfig.json'));
const APP = GC.meta.name;
const SETTINGS_CONF = `${GC.meta.restRoot.toLowerCase()}_settings`;
const tab = (name) => GC.pages.configuration.tabs.find((t) => t.name === name);
const entity = (tabName, field) => tab(tabName).entity.find((e) => e.field === field);
const label = (tabName, field) => entity(tabName, field).label;
const PAGE_TITLE = GC.pages.configuration.title;

let rest;
const saved = {};

test.beforeAll(async () => {
    rest = await ucc.restClient();
    // Every test puts these back; keep the originals to restore from.
    for (const stanza of ['receiver', 'geofence', 'logging']) {
        saved[stanza] = await rest.confStanza(APP, SETTINGS_CONF, stanza);
        expect(saved[stanza], `${SETTINGS_CONF}.conf has no [${stanza}] on a fresh install`).toBeTruthy();
    }
});

test.afterAll(async () => {
    const keys = { receiver: ['adsb_host', 'adsb_port', 'adsb_path', 'request_timeout'],
        geofence: ['center_lat', 'center_lon', 'radius_nm'], logging: ['loglevel'] };
    for (const [stanza, fields] of Object.entries(keys)) {
        const values = Object.fromEntries(fields.map((f) => [f, saved[stanza][f]]));
        await rest.setConf(APP, SETTINGS_CONF, stanza, values);
    }
    await rest.dispose();
});

test.beforeEach(async ({ page }) => {
    await ucc.login(page);
});

test('Configuration page loads every tab without app errors', async ({ page }) => {
    const errs = ucc.watchAppErrors(page, APP);
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    for (const t of GC.pages.configuration.tabs) {
        const title = t.title || (t.type === 'loggingTab' ? 'Logging' : t.name);
        await ucc.openTab(page, title);
        if (t.entity) {
            for (const e of t.entity) await expect(ucc.control(page, e.label)).toBeVisible();
        }
    }
    errs.assertNone();
});

test('Receiver tab saves, survives a reload and lands in the conf file', async ({ page }) => {
    const want = { adsb_host: 'e2e-receiver.example', adsb_port: '8181', adsb_path: '/e2e/aircraft.json',
        request_timeout: '7' };
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    await ucc.openTab(page, tab('receiver').title);
    await ucc.fillForm(page, Object.fromEntries(Object.entries(want).map(([f, v]) => [label('receiver', f), v])));
    expect(await ucc.saveConfigTab(page)).toBe(200);

    const got = await ucc.expectConf(() => rest.confStanza(APP, SETTINGS_CONF, 'receiver'),
        (c) => Object.entries(want).every(([f, v]) => c && c[f] === v), '[receiver] after Save');
    // Whole stanza: a save that wrote the edited fields and dropped others
    // would pass a field-by-field check of only what was typed.
    for (const [f, v] of Object.entries(want)) expect(got[f], f).toBe(v);

    await page.reload();
    await ucc.openTab(page, tab('receiver').title);
    for (const [f, v] of Object.entries(want)) expect(await ucc.readText(page, label('receiver', f)), f).toBe(v);
});

test('Receiver tab rejects an out-of-range port and writes nothing', async ({ page }) => {
    const before = await rest.confStanza(APP, SETTINGS_CONF, 'receiver');
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    await ucc.openTab(page, tab('receiver').title);
    await ucc.fillText(page, label('receiver', 'adsb_port'), '70000');
    await page.getByRole('button', { name: 'Save', exact: true }).click();
    const msg = entity('receiver', 'adsb_port').validators[0].errorMsg;
    await expect(page.getByText(msg).first()).toBeVisible();
    // Give a wrongly accepted save time to land before checking it did not.
    await page.waitForTimeout(3000);
    expect(await rest.confStanza(APP, SETTINGS_CONF, 'receiver')).toEqual(before);
});

test('Logging tab saves the chosen level', async ({ page }) => {
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    await ucc.openTab(page, 'Logging');
    const target = saved.logging.loglevel === 'DEBUG' ? 'WARNING' : 'DEBUG';
    await ucc.selectOption(page, page, 'Log level', target);
    expect(await ucc.saveConfigTab(page)).toBe(200);
    await ucc.expectConf(() => rest.confStanza(APP, SETTINGS_CONF, 'logging'),
        (c) => c && c.loglevel === target, `[logging] loglevel=${target} after Save`);
    // Put INFO back before the collector test, which reads INFO log lines.
    await rest.setConf(APP, SETTINGS_CONF, 'logging', { loglevel: 'INFO' });
});

test('geofence saved on the page is the geofence the collector polls with', async ({ page }) => {
    const name = `e2e_geofence_${Date.now().toString(36)}`;
    const lat = '54.4321';
    const lon = '-2.1234';
    await ucc.gotoUccPage(page, APP, 'configuration', PAGE_TITLE);
    await ucc.openTab(page, tab('geofence').title);
    await ucc.fillText(page, label('geofence', 'center_lat'), lat);
    await ucc.fillText(page, label('geofence', 'center_lon'), lon);
    expect(await ucc.saveConfigTab(page)).toBe(200);
    await ucc.expectConf(() => rest.confStanza(APP, SETTINGS_CONF, 'geofence'),
        (c) => c && c.center_lat === lat && c.center_lon === lon, '[geofence] after Save');

    // An input that sets no centre of its own, so the collector must take it
    // from the Configuration page. Created over splunkd's data/inputs endpoint.
    const since = Math.floor(Date.now() / 1000) - 5;
    const created = await rest.post(`/servicesNS/nobody/${APP}/data/inputs/airspace_watch`, {
        name, interval: '10', index: 'main', adsb_host: 'mock-receiver', adsb_port: '8080',
    });
    expect([200, 201], created.body.slice(0, 300)).toContain(created.status);
    try {
        let lines = [];
        await expect.poll(async () => {
            lines = await rest.search(
                `index=_internal source=*ta_airspace_watch.log* "[airspace_watch://${name}] poll" | table _raw`, since);
            return lines.length;
        }, { message: `no poll line from ${name} in ta_airspace_watch.log`, timeout: 180000, intervals: [10000] })
            .toBeGreaterThan(0);
        const centres = [...new Set(lines.map((l) => (/centre=\(([^)]*)\)/.exec(l._raw) || [])[1]))];
        expect(centres, `the collector polled with centre ${centres}, not the saved ${lat},${lon}`)
            .toEqual([`${Number(lat)},${Number(lon)}`]);
    } finally {
        await rest.del(`/servicesNS/nobody/${APP}/data/inputs/airspace_watch/${name}`);
    }
});
