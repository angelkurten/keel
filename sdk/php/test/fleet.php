<?php
// End to end from PHP: php test/fleet.php <baseUrl> <secret>   (the server is started by run.sh)
declare(strict_types=1);

require __DIR__ . '/../src/KeelError.php';
require __DIR__ . '/../src/Envelope.php';
require __DIR__ . '/../src/Client.php';
require __DIR__ . '/KeelFleet.php';

use Keel\Fleet\Cartridge;
use Keel\Fleet\DriverAssignedPayload;
use Keel\Fleet\Emit;
use Keel\Fleet\Keel;
use Keel\Fleet\Subjects;
use Keel\Fleet\TireDepthMeasuredPayload;
use Keel\Fleet\UnitRegisteredPayload;
use Keel\Sdk\KeelError;

function ok(bool $cond, string $what): void
{
    if (!$cond) {
        fwrite(STDERR, "FAIL: $what\n");
        exit(1);
    }
}

[$base, $secret] = [$argv[1], $argv[2]];
$tenant = 'acme-logistics';
$keel = Keel::client($base, $tenant, $secret);

$report = $keel->events([
    Emit::unitRegistered($tenant, Subjects::unit('MXK-4821'), '2026-09-01T09:00:00Z', 'fleet.vehicles', new UnitRegisteredPayload('MXK-4821', 'MXK-4821', 'Cascadia')),
    Emit::tireDepthMeasured($tenant, Subjects::tire('T-9001'), new DateTimeImmutable('2026-09-10T08:00:00Z'), 'tires', new TireDepthMeasuredPayload('MXK-4821', 'P3', 5.2)),
    Emit::tireDepthMeasured($tenant, Subjects::tire('T-9001'), '2026-10-03T08:00:00Z', 'tires', new TireDepthMeasuredPayload('MXK-4821', 'P3', 2.6)),
]);
ok($report['processed'] === 3 && $report['errors'] === [], 'ingest: ' . json_encode($report));
ok($keel->context(Subjects::tire('T-9001'))['evaluation'] === ['tire_critical' => true], 'context evaluation');
ok($keel->context(Subjects::tire('T-9001'), '2026-09-20')['evaluation'] === ['tire_critical' => false], 'as_of');
$depths = array_map(fn($f) => $f['value'], $keel->history(Subjects::tire('T-9001'), ['predicate' => 'depth_mm']));
ok($depths === [5.2, 2.6], 'history: ' . json_encode($depths));
ok($keel->related(Subjects::unit('MXK-4821'), 'mounted_on')[0]['subject'] === 'tire:T-9001', 'related');
ok(count($keel->search('T-9001')) > 0, 'search');
ok($keel->annotate(Subjects::tire('T-9001'), 'assessment', 'replace', 'task', null, 'agent:php')['scope'] === 'task', 'annotate');
try {
    $keel->annotate(Subjects::tire('T-9001'), 'assessment', 'x', 'tenant');
    ok(false, 'expected a refusal');
} catch (KeelError $e) {
    ok($e->status === 403, 'refusal status ' . $e->status);
}
$a = $keel->annotate(Subjects::tire('T-9001'), 'plan', 'swap', 'task', null, null, 'run-1', 'k1');
ok($keel->annotate(Subjects::tire('T-9001'), 'plan', 'swap', 'task', null, null, 'run-1', 'k1')['recorded_at'] === $a['recorded_at'], 'idempotent annotate');
ok(count($keel->search('', ['kind' => 'tire'])) === 1, 'search kind');
ok(count($keel->history(null, ['changed_since' => '2000-01-01T00:00:00Z', 'task' => 'run-1'])) === 1, 'changes');
ok($keel->annotate(Subjects::tire('T-9001'), 'swap', true, 'task', null, null, null, null, 'proposed')['status'] === 'proposed', 'proposal');
ok($keel->context(Subjects::tire('T-9001'), null, 0, null, null, 'concise')['facts']['depth_mm'] === 2.6, 'concise');
// the context for a task (0.19): facts and neighbours on each result, batches, hops, hidden predicates
ok($keel->search('T-9001', ['detail' => 'concise'])[0]['facts']['depth_mm'] === 2.6, 'search detail');
ok(count($keel->search('', ['kind' => 'unit', 'depth' => 1])[0]['neighbors']) > 0, 'search depth');
ok(array_column($keel->search('', ['kind' => 'tire', 'where' => ['depth_mm' => 2.6]]), 'key') === ['tire:T-9001'], 'search where');
ok(array_column($keel->search('', ['kind' => ['unit', 'tire'], 'where' => ['depth_mm' => 2.6]]), 'key') === ['tire:T-9001'], 'search kinds');
$hit = $keel->search('2.6', ['kind' => 'tire'])[0];
ok([$hit['matched'], $hit['snippet']] === ['depth_mm', '2.6'], 'search matched');
$both = $keel->context(['tire:T-9001', 'unit:MXK-4821'], null, 0, null, null, 'full', ['depth_mm']);
ok(count($both) === 2 && $both[1]['subject'] === 'unit:MXK-4821' && !in_array('depth_mm', array_column($both[0]['facts'], 'predicate'), true), 'context list with exclude');
$just = $keel->context(['tire:T-9001', 'unit:MXK-4821'], only: ['depth_mm']);
ok(array_column($just[0]['facts'], 'predicate') === ['depth_mm'] && $just[1]['facts'] === [], 'context only');
ok(count($keel->search(['T-9001', 'MXK'], ['per_kind' => 1])) === 2, 'search list');
$keel->events([Emit::driverAssigned($tenant, Subjects::unit('MXK-4821'), '2026-09-01T09:05:00Z', 'core', new DriverAssignedPayload('D-118'))]);
$hops = array_values(array_filter($keel->related('tire:T-9001', ['mounted_on', 'assigned_to'], null, 2), fn($r) => $r['subject'] === 'driver:D-118'));
ok(count($hops) === 1 && $hops[0]['hops'] === 2 && $hops[0]['via'] === 'unit:MXK-4821', 'related depth');
ok((Keel::client($base, 'someone-else', $secret)->context('tire:T-9001')['found'] ?? null) === false, 'tenant isolation');
// caps are the caller's: without a limit every entry, with one the most recent, never a {more} entry
ok(array_map(fn($f) => $f['value'], $keel->history('tire:T-9001', ['predicate' => 'depth_mm', 'limit' => 1])) === [2.6], 'history limit');
ok(array_map(fn($r) => $r['subject'], $keel->related('tire:T-9001', ['mounted_on', 'assigned_to'], null, 2, 1)) === ['unit:MXK-4821'], 'related limit');
$tp = '00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01';
ok(Keel::client($base, $tenant, $secret, null, 5, $tp)->annotate('tire:T-9001', 'traced', 'yes')['trace'] === $tp, 'client() passes traceparent');
try {
    Keel::client($base, $tenant, 'wrong')->search('x');
    ok(false, 'expected 401');
} catch (KeelError $e) {
    ok($e->status === 401, '401');
}
ok(Cartridge::NAME === 'fleet' && count(Cartridge::KINDS) === 7 && Cartridge::RULES['tire_critical']['threshold_mm'] === 3, 'constants');
echo "php sdk ok\n";
