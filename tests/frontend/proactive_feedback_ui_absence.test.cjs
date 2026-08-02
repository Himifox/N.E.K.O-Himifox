const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const projectRoot = path.resolve(__dirname, '..', '..');

function read(relativePath) {
  return fs.readFileSync(path.join(projectRoot, relativePath), 'utf8');
}

test('proactive recommendations do not render explicit feedback actions', () => {
  const frontend = read(path.join('static', 'app', 'app-proactive.js'));
  const integration = read(path.join('main_logic', 'proactive_chat', 'recommendation_integration.py'));
  const settings = read(path.join('config', 'proactive_settings.py'));

  assert.doesNotMatch(frontend, /proactive_scoped_feedback|_showProactiveFeedbackActions/);
  assert.doesNotMatch(integration, /feedback_context|_explicit_feedback_context/);
  assert.doesNotMatch(settings, /PROACTIVE_RECOMMENDATION_EXPLICIT_FEEDBACK_UI/);
});
