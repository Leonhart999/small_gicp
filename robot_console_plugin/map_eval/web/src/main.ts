// Entry of the map quality evaluation frontend: built into ../ui/plugin.js with npm run build in web/.
// vue and element-plus come from window.RobotConsole at run time.
import Page from './Page.vue'
import { messages } from './messages'

window.RobotConsole.register({
  id: 'map_eval',
  messages,
  pages: [{ path: 'main', title: { zh: '地图质量评估', en: 'Map evaluation' }, component: Page }],
})
