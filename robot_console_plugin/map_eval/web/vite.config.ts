// Builds this plugin's page into ../ui/plugin.js + plugin.css (one IIFE script). vue and element-plus are not
// bundled: at run time the plugin uses the console's own copies through window.RobotConsole (plugin interface v1).
import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  define: { 'process.env.NODE_ENV': JSON.stringify('production') },
  build: {
    outDir: '../ui',
    emptyOutDir: true,
    copyPublicDir: false,
    cssCodeSplit: false,
    minify: true,
    lib: {
      entry: 'src/main.ts',
      formats: ['iife'],
      name: 'RobotConsolePlugin_map_eval',
      fileName: () => 'plugin.js',
      cssFileName: 'plugin',
    },
    rollupOptions: {
      external: ['vue', 'element-plus'],
      output: { globals: { vue: 'RobotConsole.vue', 'element-plus': 'RobotConsole.ui' } },
    },
  },
})
