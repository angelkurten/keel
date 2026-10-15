<?php
// End to end from PHP: php test/fleet.php <baseUrl> <secret>   (the server is started by run.sh)
declare(strict_types=1);

require __DIR__ . '/../src/KeelError.php';
require __DIR__ . '/../src/Envelope.php';
require __DIR__ . '/../src/Client.php';
require __DIR__ . '/KeelFleet.php';

use Keel\Fleet\Cartridge;
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
ok(Keel::client($base, 'someone-else', $secret)->context('tire:T-9001')['entity'] === null, 'tenant isolation');
try {
    Keel::client($base, $tenant, 'wrong')->search('x');
    ok(false, 'expected 401');
} catch (KeelError $e) {
    ok($e->status === 401, '401');
}
ok(Cartridge::NAME === 'fleet' && count(Cartridge::KINDS) === 7 && Cartridge::RULES['tire_critical']['threshold_mm'] === 3, 'constants');
echo "php sdk ok\n";
