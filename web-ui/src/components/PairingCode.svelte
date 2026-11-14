<script lang="ts">
  import { onMount } from 'svelte';
  import QRCode from 'qrcode';
  import type { PairingSecret } from '../lib/types.ts';
  let { secret, onhide }: { secret: PairingSecret; onhide: () => void } = $props();
  let canvas: HTMLCanvasElement;
  let failed = $state(false);
  let remaining = $state(60);
  onMount(() => {
    let active = true;
    void QRCode.toCanvas(canvas, secret.setup_uri, {
      width: 224,
      margin: 3,
      errorCorrectionLevel: 'M',
    }).catch(() => {
      if (active) failed = true;
    });
    const expires = Date.now() + Math.min(secret.expires_in_ms, 60_000);
    const timer = setInterval(() => {
      remaining = Math.max(0, Math.ceil((expires - Date.now()) / 1000));
      if (!remaining) onhide();
    }, 250);
    return () => {
      active = false;
      clearInterval(timer);
      canvas?.getContext('2d')?.clearRect(0, 0, 224, 224);
    };
  });
</script>

<section class="pairing-secret" aria-label="HomeKit pairing code">
  <canvas bind:this={canvas} aria-label="Scan this pairing QR code using Apple Home"></canvas>
  {#if failed}<p>The QR image is unavailable. Enter the code below in Apple Home.</p>{/if}
  <p class="pairing-pin">{secret.pin}</p>
  <p class="muted">
    Hidden automatically in {remaining} seconds. Hiding does not rotate this code.
  </p>
  <button onclick={onhide}>Hide pairing code</button>
</section>
