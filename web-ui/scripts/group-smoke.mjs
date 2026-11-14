/** Exercise the shared light UI with the strict group DTO and explicit import contract. */
process.env.HALO_LIGHT_KIND = 'group';
await import('./light-smoke.mjs');
