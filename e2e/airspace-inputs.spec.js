/*
 * Inputs page of TA-airspace-watch, driven in a browser on a real Splunk:
 * create, disable, enable, edit and delete one input of every service in
 * globalConfig.json. After each step the input's stanza is read from
 * splunkd's raw configs/conf-inputs endpoint, which the page and the add-on's
 * REST handler never read, so a form that maps a label to the wrong key, or a
 * handler that drops a write, fails here.
 */
const path = require('path');
const { test, expect } = require('@playwright/test');
const ucc = require('./ucc/ucc-helpers');

const GC = require(path.join(__dirname, '..', 'globalConfig.json'));
const APP = GC.meta.name;
const PAGE_TITLE = GC.pages.inputs.title;

// What the page should save, per service: the visible label typed into and the
// inputs.conf key it must land under. Written out here, not looked up from
// globalConfig.json by field name: a lookup by field would follow a renamed or
// remapped field and agree with the bug (a mutant that did exactly that went red
// in the lookup, not in the assertion). The receiver host is the compose
// network's mock, so an enabled input polls something real.
const FIELDS = {
    airspace_watch: {
        name: 'Name', interval: 'Interval (s)', index: 'Index', sourcetype: 'Sourcetype',
        adsb_host: 'ADS-B host (override)', adsb_port: 'ADS-B port (override)', radius_nm: 'Radius (nm, override)',
    },
};
const VALUES = {
    airspace_watch: {
        create: { interval: '30', index: 'main', sourcetype: 'airspace:adsb', adsb_host: 'mock-receiver',
            adsb_port: '8080', radius_nm: '42' },
        edit: { interval: '45', adsb_port: '8090', radius_nm: '17' },
    },
};

let rest;
test.beforeAll(async () => {
    rest = await ucc.restClient();
});
test.afterAll(async () => rest.dispose());
test.beforeEach(async ({ page }) => ucc.login(page));

test('every input service has test values', () => {
    expect(GC.pages.inputs.services.map((s) => s.name).sort()).toEqual(Object.keys(VALUES).sort());
});

for (const service of GC.pages.inputs.services) {
    const label = (key) => FIELDS[service.name][key];
    const byLabel = (vals) => Object.fromEntries(Object.entries(vals).map(([f, v]) => [label(f), v]));
    const stanza = (name) => rest.inputStanza(APP, service.name, name);

    test(`${service.name}: create, disable, enable, edit and delete through the page`, async ({ page }) => {
        const errs = ucc.watchAppErrors(page, APP);
        const name = `e2e_${service.name}_${Date.now().toString(36)}`;
        const { create, edit } = VALUES[service.name];
        try {
            await ucc.gotoUccPage(page, APP, 'inputs', PAGE_TITLE);

            // Create
            const d = await ucc.openCreateInput(page, service.title);
            await ucc.fillForm(d, byLabel({ name, ...create }));
            await ucc.submitDialog(page, 'Add');
            await expect(ucc.inputRow(page, name)).toBeVisible();
            const made = await ucc.expectConf(() => stanza(name), (c) => c !== null, `[${service.name}://${name}] created`);
            for (const [f, v] of Object.entries(create)) expect(made[f], `created ${f}`).toBe(v);
            expect(ucc.isTrue(made.disabled), 'a new input starts enabled').toBe(false);

            // Disable, then enable
            await ucc.toggleRow(page, name);
            await ucc.expectConf(() => stanza(name), (c) => c && ucc.isTrue(c.disabled), 'disabled after the switch');
            await ucc.toggleRow(page, name);
            await ucc.expectConf(() => stanza(name), (c) => c && !ucc.isTrue(c.disabled), 'enabled after the switch');

            // Edit: the dialog opens with what was saved, and the update lands
            await ucc.rowAction(page, name, 'Edit');
            const e = ucc.dialog(page);
            await expect(e).toBeVisible();
            for (const [f, v] of Object.entries(create)) expect(await ucc.readText(e, label(f)), `edit shows ${f}`).toBe(v);
            await ucc.fillForm(e, byLabel(edit));
            await ucc.submitDialog(page, 'Update');
            const want = { ...create, ...edit };
            const after = await ucc.expectConf(() => stanza(name),
                (c) => c && Object.entries(edit).every(([f, v]) => c[f] === v), 'edited values saved');
            // Whole stanza: the fields not edited must survive the update.
            for (const [f, v] of Object.entries(want)) expect(after[f], `after edit ${f}`).toBe(v);

            // Delete
            await ucc.rowAction(page, name, 'Delete');
            await ucc.submitDialog(page, 'Delete');
            await expect(ucc.inputRow(page, name)).toHaveCount(0);
            await ucc.expectConf(() => stanza(name), (c) => c === null, `[${service.name}://${name}] removed`);
            errs.assertNone();
        } finally {
            await rest.deleteInput(APP, service.name, name);
        }
    });

    test(`${service.name}: an invalid name is refused and nothing is written`, async ({ page }) => {
        const bad = '9_starts_with_a_digit';
        await ucc.gotoUccPage(page, APP, 'inputs', PAGE_TITLE);
        const d = await ucc.openCreateInput(page, service.title);
        await ucc.fillForm(d, byLabel({ name: bad, ...VALUES[service.name].create }));
        await d.getByRole('button', { name: 'Add', exact: true }).click();
        const msg = service.entity.find((x) => x.field === 'name').validators[0].errorMsg;
        await expect(d.getByText(msg).first()).toBeVisible();
        await page.waitForTimeout(3000);
        expect(await stanza(bad)).toBeNull();
        await d.getByRole('button', { name: 'Cancel', exact: true }).click();
    });
}
