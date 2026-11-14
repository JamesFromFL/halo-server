<script lang="ts">
  let { name = 'camera', size = 20 }: { name?: string; size?: number } = $props();
  const paths: Record<string, string> = {
    overview: 'M3 3h7v7H3z M14 3h7v7h-7z M3 14h7v7H3z M14 14h7v7h-7z',
    camera: 'M3 6h13v12H3z M16 10l5-3v10l-5-3',
    devices: 'M3 4h18v12H3z M8 21h8 M12 16v5',
    integrations: 'M8 3v4 M16 3v4 M5 7h14v5a7 7 0 0 1-14 0z M12 19v3',
    activity: 'M3 12h4l3-8 4 16 3-8h4',
    diagnostics: 'M8 3v5l-5 9a3 3 0 0 0 3 4h12a3 3 0 0 0 3-4l-5-9V3 M6 3h12 M7 14h10',
    settings: 'M9 3h6l1 4 4 1v8l-4 1-1 4H9l-1-4-4-1V8l4-1z M12 9a3 3 0 1 0 0 6 3 3 0 0 0 0-6',
    menu: 'M4 6h16 M4 12h16 M4 18h16',
    close: 'M6 6l12 12 M18 6L6 18',
    back: 'M14 5l-7 7 7 7',
    next: 'M9 5l7 7-7 7',
    check: 'M4 12l5 5L20 6',
    warning: 'M12 3L2 21h20z M12 9v5 M12 17v1',
    account: 'M12 3a4 4 0 1 0 0 8 4 4 0 0 0 0-8 M4 21v-2a8 8 0 0 1 16 0v2',
    lock: 'M6 10h12v11H6z M8 10V7a4 4 0 0 1 8 0v3 M12 14v3',
    server: 'M3 3h18v7H3z M3 14h18v7H3z M6 6h1 M6 17h1 M16 6h2 M16 17h2',
    refresh: 'M20 7v5h-5 M4 17v-5h5 M5 7a8 8 0 0 1 14 0 M19 17a8 8 0 0 1-14 0',
    light: 'M8 17h8 M9 21h6 M8 14a6 6 0 1 1 8 0v3H8z',
    bell: 'M5 17h14l-2-3V9a5 5 0 0 0-10 0v5z M10 21h4',
    shield: 'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6z M8 12l3 3 5-6',
    contact: 'M4 3h10v18H4z M18 5h3v14h-3z M11 12h.01',
  };
</script>

<svg
  width={size}
  height={size}
  viewBox="0 0 24 24"
  fill="none"
  stroke="currentColor"
  stroke-width="1.8"
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"><path d={paths[name] ?? paths.camera} /></svg
>
