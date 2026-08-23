export default {
  content: ['./index.html', './src/**/*.{jsx,js}'],
  theme: {
    extend: {
      colors: {
        'bg-primary': '#131313',
        'bg-secondary': '#1c1c1c',
        'bg-card': '#1c1c1c',
        'accent-cyan': '#ff8a3d',
        'accent-orange': '#ff8a3d',
        'accent-green': '#00ff9d',
        'accent-red': '#ff2d2d',
        'accent-gold': '#ffd700',
        'accent-blue': '#0a2a4a',
        'text-primary': '#f4f3f0',
        'text-secondary': '#98958c',
        'border-dark': '#2c2c2a',
      },
      fontFamily: {
        mono: ['IBM Plex Mono', 'monospace'],
        sans: ['Archivo', 'system-ui', 'sans-serif'],
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
