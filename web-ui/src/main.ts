import { flushSync, mount } from 'svelte';
import Root from './Root.svelte';
import './theme.css';
const target = document.getElementById('app');
if (target) {
  try {
    flushSync(() => mount(Root, { target }));
    document.getElementById('startup-status')?.remove();
  } catch {
    // Keep the static recovery link usable even if the framework cannot mount.
    target.replaceChildren();
    const title = document.getElementById('startup-title');
    if (title) title.textContent = 'Halo could not start its interface';
  }
}
