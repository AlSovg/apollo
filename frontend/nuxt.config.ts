// Фронтенд собирается в статику и раздаётся nginx — SSR отключён осознанно:
// в продуктивном контуре не должно остаться Node-процесса (раздел 2 AGENTS.md).
export default defineNuxtConfig({
  ssr: false,
  devtools: { enabled: false },
  modules: ['@pinia/nuxt'],
  css: ['~/assets/styles/main.css'],
  typescript: {
    strict: true,
  },
  // Учётки для кнопок быстрого входа на демо-стенде. Значения попадают в
  // статику на этапе сборки, поэтому по умолчанию пусто — без них кнопок
  // на экране входа просто нет, и боевая сборка не несёт никаких паролей.
  runtimeConfig: {
    public: {
      demoUser: process.env.NUXT_PUBLIC_DEMO_USER ?? '',
      demoAdmin: process.env.NUXT_PUBLIC_DEMO_ADMIN ?? '',
    },
  },
  app: {
    head: {
      title: 'Платформа управления оповещениями',
      htmlAttrs: { lang: 'ru' },
      meta: [
        { charset: 'utf-8' },
        { name: 'viewport', content: 'width=device-width, initial-scale=1' },
      ],
      // Иконки лежат в public/ и раздаются тем же nginx — внешних CDN нет
      link: [
        { rel: 'icon', href: '/favicon.ico', sizes: 'any' },
        { rel: 'icon', type: 'image/png', sizes: '32x32', href: '/favicon-32.png' },
        { rel: 'apple-touch-icon', href: '/apple-touch-icon.png' },
      ],
    },
  },
  // Прокси на бэкенд нужен только в режиме разработки: в проде тот же nginx,
  // что раздаёт статику, проксирует /api
  vite: {
    server: {
      proxy: {
        '/api': {
          target: process.env.NUXT_DEV_API ?? 'http://localhost',
          changeOrigin: true,
        },
      },
    },
  },
})
