# keel-context/sdk (PHP)

Runtime for the clients `keel sdk php <cartridge> --package '<Namespace>'` generates: the event envelope and the five Keel tools over HTTP (`keel serve --transport http`). PHP 8.1+, no dependencies (curl when present, streams otherwise).

```bash
composer require keel-context/sdk
keel sdk php cartridges/fleet -o src/Keel/Fleet.php --package 'App\Keel\Fleet'
```

```php
$keel = \App\Keel\Fleet\Keel::client('http://keel.internal:8000', 'acme-logistics', getenv('KEEL_SECRET'));
$keel->events([
    \App\Keel\Fleet\Emit::tireDepthMeasured('acme-logistics', \App\Keel\Fleet\Subjects::tire('T-9001'), new DateTimeImmutable(), 'tires', new \App\Keel\Fleet\TireDepthMeasuredPayload('MXK-4821', 'P3', 2.6)),
]);
$keel->context(\App\Keel\Fleet\Subjects::tire('T-9001'))['evaluation']['tire_critical']; // true
```

The generated `Keel::client()` takes base URL, tenant, secret and agent. For a timeout (seconds, default 30) or a `traceparent` (sent as the W3C header on every call; Keel stores it with every write), construct the runtime client directly:

```php
$keel = new \Keel\Sdk\Client('http://keel.internal:8000', 'acme-logistics', getenv('KEEL_SECRET'), timeout: 10, traceparent: $traceparent);
```

A refusal from the server is a `Keel\Sdk\KeelError` with `$status` and `$detail`. Payload classes are readonly value objects with `toArray()`; enums in the cartridge become PHP enums.
