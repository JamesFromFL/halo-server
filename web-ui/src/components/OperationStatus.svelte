<script lang="ts">
  import { api, errorMessage } from '../lib/api.ts';
  import { pretty } from '../lib/model.ts';
  import type { Operation } from '../lib/types.ts';
  let {
    operation,
    onupdate,
    configuration = false,
    pairing = false,
    alarm = false,
    light = false,
  }: {
    operation: Operation;
    onupdate: (v: Operation) => void;
    configuration?: boolean;
    pairing?: boolean;
    alarm?: boolean;
    light?: boolean;
  } = $props();
  let checking = $state(false);
  let error = $state('');
  async function check() {
    if (!operation.operation_id) return;
    checking = true;
    try {
      onupdate(await api.get<Operation>(`operations/${operation.operation_id}`));
      error = '';
    } catch (e) {
      error = errorMessage(e);
    } finally {
      checking = false;
    }
  }
  const resultStatus = $derived((operation.result as Record<string, unknown> | undefined)?.status);
</script>

<div class="notice operation" role="status">
  <strong>{pretty(resultStatus ?? operation.status ?? operation.outcome)}</strong>
  <p>
    {operation.status === 'unknown' ||
    operation.possibly_sent ||
    ((alarm || light) && resultStatus === 'uncertain')
      ? pairing
        ? 'Halo may have changed this HomeKit pairing. The outcome is uncertain; the reset will not be sent again automatically.'
        : configuration
          ? 'Halo may have saved these preferences. The outcome is uncertain; the change will not be sent again automatically.'
          : 'The action may have reached the device. Its outcome is uncertain; it will not be sent again automatically.'
      : alarm && resultStatus === 'confirmed'
        ? 'Ring confirmed the requested alarm mode. The observed state above is reported separately.'
        : light && resultStatus === 'confirmed'
          ? 'Ring confirmed the light request. Its observed illumination is reported separately.'
          : resultStatus === 'accepted'
            ? 'Ring accepted the command. The observed device state may update later.'
            : operation.status === 'pending'
              ? 'Halo is still finishing this action.'
              : (operation.message ?? 'The server has reported this operation result.')}
  </p>
  {#if operation.code}<p class="caption">Reason: {pretty(operation.code)}</p>{/if}
  {#if operation.operation_id}<small class="wrap">Operation: {operation.operation_id}</small
    >{/if}{#if operation.status === 'unknown' || operation.status === 'pending' || ((alarm || light) && resultStatus === 'uncertain')}<div
      class="actions"
    >
      <button disabled={checking} onclick={check}>{checking ? 'Checking…' : 'Check outcome'}</button
      >
    </div>{/if}{#if error}<p class="negative">{error}</p>{/if}
</div>
