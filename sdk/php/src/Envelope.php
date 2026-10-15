<?php
declare(strict_types=1);

namespace Keel\Sdk;

/** The standard event envelope, as an array. `id` is optional: the server derives a content hash when absent. */
final class Envelope
{
    /** @param array<string, mixed> $payload @return array<string, mixed> */
    public static function make(string $type, string $tenant, string $subject, \DateTimeInterface|string $occurredAt, string $source, array $payload, ?string $id = null, \DateTimeInterface|string|null $recordedAt = null): array
    {
        $e = ['tenant' => $tenant, 'type' => $type, 'subject' => $subject, 'occurred_at' => self::iso($occurredAt), 'source' => $source, 'payload' => $payload];
        if ($id !== null) {
            $e['id'] = $id;
        }
        if ($recordedAt !== null) {
            $e['recorded_at'] = self::iso($recordedAt);
        }
        return $e;
    }

    public static function iso(\DateTimeInterface|string $d): string
    {
        return $d instanceof \DateTimeInterface ? $d->format(\DateTimeInterface::ATOM) : $d;
    }
}
