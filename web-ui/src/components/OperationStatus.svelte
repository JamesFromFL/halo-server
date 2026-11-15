<script lang="ts">
  import { onDestroy } from 'svelte';
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
    chime = false,
    keypad = false,
  }: {
    operation: Operation;
    onupdate: (v: Operation) => void;
    configuration?: boolean;
    pairing?: boolean;
    alarm?: boolean;
    light?: boolean;
    chime?: boolean;
    keypad?: boolean;
  } = $props();
  let checking = $state(false);
  let error = $state('');
  let observedOperation = '';
  let serial = 0;
  let destroyed = false;
  $effect(() => {
    const id = operation.operation_id ?? '';
    if (observedOperation !== id) {
      observedOperation = id;
      serial++;
      checking = false;
      error = '';
    }
  });
  onDestroy(() => {
    destroyed = true;
    serial++;
  });
  async function check() {
    const id = operation.operation_id;
    if (!id || checking) return;
    const request = ++serial;
    checking = true;
    error = '';
    try {
      const result = await api.get<Operation>(`operations/${encodeURIComponent(id)}`);
      if (!destroyed && request === serial && operation.operation_id === id) onupdate(result);
    } catch (e) {
      if (!destroyed && request === serial && operation.operation_id === id)
        error = errorMessage(e);
    } finally {
      if (!destroyed && request === serial) checking = false;
    }
  }
  const resultStatus = $derived((operation.result as Record<string, unknown> | undefined)?.status);
</script>

<div class="notice operation" role="status">
  <strong>{pretty(resultStatus ?? operation.status ?? operation.outcome)}</strong>
  <p>
    {operation.status === 'unknown' ||
    operation.possibly_sent ||
    ((alarm || light || chime || keypad) && resultStatus === 'uncertain')
      ? pairing
        ? 'Halo may have changed this HomeKit pairing. The outcome is uncertain; the reset will not be sent again automatically.'
        : configuration
          ? 'Halo may have saved these preferences. The outcome is uncertain; the change will not be sent again automatically.'
          : 'The action may have reached the device. Its outcome is uncertain; it will not be sent again automatically.'
      : alarm && resultStatus === 'confirmed'
        ? 'Ring confirmed the requested alarm mode. The observed state above is reported separately.'
        : keypad && resultStatus === 'confirmed'
          ? 'Ring confirmed the configured keypad brightness. The reported level is shown above.'
          : light && resultStatus === 'confirmed'
            ? 'Ring confirmed the light request. Its observed illumination is reported separately.'
            : chime && resultStatus === 'confirmed'
              ? 'The server confirmed this request. Ring observations are shown separately above.'
              : chime && resultStatus === 'accepted'
                ? 'Ring accepted the sound request. This does not confirm audible playback or completion; listen before acknowledging that the sound has stopped.'
                : resultStatus === 'accepted'
                  ? 'Ring accepted the command. The observed device state may update later.'
                  : operation.status === 'pending'
                    ? 'Halo is still finishing this action.'
                    : (operation.message ?? 'The server has reported this operation result.')}
  </p>
  {#if operation.code}<p class="caption">Reason: {pretty(operation.code)}</p>{/if}
  {#if operation.operation_id}<small class="wrap">Operation: {operation.operation_id}</small
    >{/if}{#if operation.status === 'unknown' || operation.status === 'pending' || ((alarm || light || chime || keypad) && resultStatus === 'uncertain')}<div
      class="actions"
    >
      <button disabled={checking} onclick={check}>{checking ? 'Checking…' : 'Check outcome'}</button
      >
    </div>{/if}{#if error}<p class="negative">{error}</p>{/if}
</div>
