<?php
declare(strict_types=1);

namespace Keel\Sdk;

/** The five tools plus `events` (ingest), bound to one tenant and optionally one agent. Talks to `keel serve --transport http`. */
final class Client
{
    public function __construct(
        private readonly string $baseUrl,
        private readonly string $tenant,
        private readonly ?string $secret = null,
        private readonly ?string $agent = null,
        private readonly int $timeout = 30,
        private readonly ?string $traceparent = null,
    ) {
    }

    /** @return array<string, mixed> */
    /** `$depth = 1` adds each neighbour's entity, open facts and evaluation under `neighbors`; `$asKnownAt` answers
     * what Keel knew then; `$task` keeps one task's memory; `$exclude` leaves predicates out. The result's `watermark`
     * is the changes cursor. A list of subjects returns a list of answers, in order.
     * @param string|list<string> $subject @param list<string>|null $exclude */
    public function context(string|array $subject, \DateTimeInterface|string|null $asOf = null, int $depth = 0, \DateTimeInterface|string|null $asKnownAt = null, ?string $task = null, string $detail = 'full', ?array $exclude = null): array
    {
        return $this->post('/v1/context', [
            'subject' => $subject,
            'as_of' => $asOf === null ? null : Envelope::iso($asOf),
            'depth' => $depth,
            'as_known_at' => $asKnownAt === null ? null : Envelope::iso($asKnownAt),
            'task' => $task,
            'detail' => $detail,
            'exclude' => $exclude,
        ]);
    }

    /** `$subject = null` with `changed_since` lists the tenant's changes since a watermark.
     * @param array{since?: \DateTimeInterface|string, until?: \DateTimeInterface|string, predicate?: string, scope?: string, as_known_at?: \DateTimeInterface|string, task?: string, changed_since?: \DateTimeInterface|string} $filter @return list<array<string, mixed>> */
    public function history(?string $subject, array $filter = []): array
    {
        $iso = fn(string $k) => isset($filter[$k]) ? Envelope::iso($filter[$k]) : null;
        return $this->post('/v1/history', [
            'subject' => $subject,
            'since' => $iso('since'),
            'until' => $iso('until'),
            'predicate' => $filter['predicate'] ?? null,
            'scope' => $filter['scope'] ?? null,
            'as_known_at' => $iso('as_known_at'),
            'task' => $filter['task'] ?? null,
            'changed_since' => $iso('changed_since'),
            'limit' => $filter['limit'] ?? null,
        ]);
    }

    /** With `kind` or `rule`: the entities of a kind, or where a rule fires now, paged by `limit`/`after`.
     * `mode` => 'semantic' ranks by meaning, each result with a `score` and `similarity`; `min_score` drops results below a cosine.
     * `related_to` (+ `via`) keeps only that subject's neighbours. `detail` => 'concise' puts each result's facts in it,
     * `depth` => 1 its neighbours, `per_kind` => n the best of every kind (with `related_to` and semantic mode, the
     * context for a task); `exclude` leaves predicates out; `as_of` evaluates at that instant. A list of texts returns
     * one result list per text.
     * @param string|list<string> $text
     * @param array{kind?: string, rule?: string, limit?: int, after?: string, mode?: string, min_score?: float, related_to?: string, via?: string, detail?: string, depth?: int, per_kind?: int, exclude?: list<string>, as_of?: \DateTimeInterface|string} $filter @return list<mixed> */
    public function search(string|array $text = '', array $filter = []): array
    {
        if (isset($filter['as_of'])) {
            $filter['as_of'] = Envelope::iso($filter['as_of']);
        }
        return $this->post('/v1/search', ['text' => $text] + $filter);
    }

    /** `$task` ties the fact to a job; `$key` makes a retried write return the first one. @return array<string, mixed> */
    public function annotate(string $subject, string $predicate, mixed $value, string $scope = 'task', ?string $evidence = null, ?string $by = null, ?string $task = null, ?string $key = null, ?string $status = null): array
    {
        return $this->post('/v1/annotate', ['subject' => $subject, 'predicate' => $predicate, 'value' => $value, 'scope' => $scope, 'evidence' => $evidence, 'by' => $by, 'task' => $task, 'key' => $key, 'status' => $status]);
    }

    /** Neighbours by relation (one, or a list); `$depth` 2 or 3 walks further, each entry with `hops` and `via`.
     * @param string|list<string>|null $relation @return list<array<string, mixed>> */
    public function related(string $subject, string|array|null $relation = null, \DateTimeInterface|string|null $asOf = null, int $depth = 1): array
    {
        return $this->post('/v1/related', ['subject' => $subject, 'relation' => $relation, 'as_of' => $asOf === null ? null : Envelope::iso($asOf), 'depth' => $depth]);
    }

    /** @param list<array<string, mixed>> $batch @return array<string, mixed> */
    public function events(array $batch): array
    {
        return $this->post('/v1/events', $batch);
    }

    private function post(string $path, mixed $body): mixed
    {
        $headers = ["Content-Type: application/json", "X-Keel-Tenant: {$this->tenant}"];
        if ($this->agent !== null) {
            $headers[] = "X-Keel-Agent: {$this->agent}";
        }
        if ($this->secret !== null) {
            $headers[] = "Authorization: Bearer {$this->secret}";
        }
        if ($this->traceparent !== null) {
            $headers[] = "traceparent: {$this->traceparent}";
        }
        $json = json_encode($body, JSON_THROW_ON_ERROR | JSON_UNESCAPED_UNICODE);
        $url = rtrim($this->baseUrl, '/') . $path;
        if (function_exists('curl_init')) {
            $ch = curl_init($url);
            curl_setopt_array($ch, [CURLOPT_POST => true, CURLOPT_POSTFIELDS => $json, CURLOPT_HTTPHEADER => $headers, CURLOPT_RETURNTRANSFER => true, CURLOPT_TIMEOUT => $this->timeout]);
            $raw = curl_exec($ch);
            if ($raw === false) {
                throw new \RuntimeException('keel: ' . curl_error($ch));
            }
            $status = (int) curl_getinfo($ch, CURLINFO_RESPONSE_CODE);
            curl_close($ch);
        } else {
            $ctx = stream_context_create(['http' => ['method' => 'POST', 'header' => implode("\r\n", $headers), 'content' => $json, 'timeout' => $this->timeout, 'ignore_errors' => true]]);
            $raw = file_get_contents($url, false, $ctx);
            if ($raw === false) {
                throw new \RuntimeException("keel: no answer from $url");
            }
            $status = isset($http_response_header[0]) && preg_match('/\s(\d{3})\s/', $http_response_header[0], $m) ? (int) $m[1] : 0;
        }
        if ($status < 200 || $status >= 300) {
            $decoded = json_decode((string) $raw, true);
            $detail = is_array($decoded) && isset($decoded['detail']) ? $decoded['detail'] : (string) $raw;
            throw new KeelError($status, is_string($detail) ? $detail : json_encode($detail));
        }
        return json_decode((string) $raw, true, 512, JSON_THROW_ON_ERROR);
    }
}
