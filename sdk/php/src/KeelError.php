<?php
declare(strict_types=1);

namespace Keel\Sdk;

/** A non-2xx answer: a refusal (403: scope policy, agent registry), bad input (400), bad secret (401). */
final class KeelError extends \RuntimeException
{
    public function __construct(public readonly int $status, public readonly string $detail)
    {
        parent::__construct("keel $status: $detail", $status);
    }
}
