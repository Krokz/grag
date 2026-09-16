import {defineConfig} from '@playwright/test';

const port=process.env.GRAG_UI_TEST_PORT || '47836';
export default defineConfig({
  testDir:'./tests',workers:1,timeout:30000,
  use:{baseURL:`http://127.0.0.1:${port}`,trace:'retain-on-failure',actionTimeout:10000,
    ...(process.env.GRAG_UI_TEST_CHROME ? {channel:'chrome'} : {})},
  webServer:{command:`${process.env.GRAG_UI_TEST_PYTHON || 'python'} ../scripts/serve_ui_fixture.py`,
    url:`http://127.0.0.1:${port}/api/health`,reuseExistingServer:false,timeout:60000},
});
