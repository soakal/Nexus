export default {
  content: ['./index.html', './src/**/*.{jsx,js}'],
  theme: {
    extend: {
      colors: {
        'bg-primary': '#0a0e1a',
        'bg-secondary': '#0f1528',
        'bg-card': '#141d35',
        'accent-cyan': '#00d4ff',
        'accent-orange': '#ff6b2b',
        'accent-green': '#00ff88',
        'text-primary': '#e8edf8',
        'text-secondary': '#7a8aaa',
        'border-dark': '#1e2d4a',
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'monospace'],
        sans: ['DM Sans', 'sans-serif'],
      },
    },
  },
  plugins: [],
}
