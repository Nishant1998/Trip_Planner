-- Run manually with the admin database connection. Replace the example email.
-- Grant unlimited usage without granting the admin role.
UPDATE users
SET preferences = jsonb_set(COALESCE(preferences, '{}'::jsonb),
                           '{demo_access}', '"full"'::jsonb)
WHERE lower(email) = lower('replace-with-account@example.com')
RETURNING email, preferences->>'demo_access' AS access;

-- To restore demo allowances, run this statement instead:
-- UPDATE users SET preferences = COALESCE(preferences, '{}'::jsonb) - 'demo_access'
-- WHERE lower(email) = lower('replace-with-account@example.com')
-- RETURNING email;
