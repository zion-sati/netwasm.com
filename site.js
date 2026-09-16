const root = document.documentElement;
const themeButton = document.querySelector('.theme-toggle');

const storedTheme = localStorage.getItem('netwasm-theme');
if (storedTheme === 'light' || storedTheme === 'dark') {
  root.dataset.theme = storedTheme;
}

themeButton?.addEventListener('click', () => {
  const systemDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
  const currentDark = root.dataset.theme ? root.dataset.theme === 'dark' : systemDark;
  const next = currentDark ? 'light' : 'dark';
  root.dataset.theme = next;
  localStorage.setItem('netwasm-theme', next);
});

document.querySelectorAll('[data-copy-target]').forEach((button) => {
  button.addEventListener('click', async () => {
    const target = document.getElementById(button.dataset.copyTarget);
    if (!target) return;
    const text = target.textContent.replace(/^\$ /gm, '').replace(/^42$/m, '').replace(/\n\n+/g, '\n').trim();
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      const input = document.createElement('textarea');
      input.value = text;
      input.setAttribute('readonly', '');
      input.style.position = 'fixed';
      input.style.opacity = '0';
      document.body.appendChild(input);
      input.select();
      document.execCommand('copy');
      input.remove();
    }
    const previous = button.textContent;
    button.textContent = 'Copied';
    window.setTimeout(() => { button.textContent = previous; }, 1600);
  });
});
