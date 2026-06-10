export default {
  content: ['./index.html', './src/**/*.{jsx,js}'],
  theme: {
    extend: {
      colors: {
        'bg-primary': '#04080f',
        'bg-secondary': '#060c16',
        'bg-card': '#080f1e',
        'accent-cyan': '#00d4ff',
        'accent-orange': '#ff9500',
        'accent-green': '#00ff9d',
        'accent-red': '#ff2d2d',
        'accent-gold': '#ffd700',
        'accent-blue': '#0a2a4a',
        'text-primary': '#cce5f0',
        'text-secondary': '#4d7c96',
        'border-dark': '#0c2035',
      },
      fontFamily: {
        mono: ['JetBrains Mono', 'monospace'],
        sans: ['DM Sans', 'sans-serif'],
        orbitron: ['Orbitron', 'sans-serif'],
      },
      animation: {
        'pulse-glow': 'pulse-glow 2s ease-in-out infinite',
        'data-flicker': 'data-flicker 3s ease-in-out infinite',
        'scan-sweep': 'scan-sweep 4s linear infinite',
      },
      keyframes: {
        'pulse-glow': {
          '0%,100%': { opacity: '1' },
          '50%': { opacity: '0.6' },
        },
        'data-flicker': {
          '0%,100%': { opacity: '1' },
          '50%': { opacity: '0.85' },
          '75%': { opacity: '0.95' },
        },
        'scan-sweep': {
          '0%': { transform: 'translateY(-100%)' },
          '100%': { transform: 'translateY(100%)' },
        },
      },
    },
  },
  plugins: [],
}
