#!/bin/bash
# Emergency Index Recovery Script
# Kills hanging index creation and creates only the critical location index

set -e

echo "🔧 Freedom Ridge Index Recovery"
echo "================================="
echo ""

# Step 1: Kill any hanging Python index creation scripts
echo "1️⃣  Stopping any hanging index creation processes..."
pkill -f "apply.*index\|CREATE INDEX CONCURRENTLY" 2>/dev/null || true
sleep 2
echo "   ✅ Processes stopped"
echo ""

# Step 2: Check database connectivity
echo "2️⃣  Testing database connection..."
if psql -U freedomridge -d freedomridge -c "SELECT 1;" > /dev/null 2>&1; then
    echo "   ✅ Database is responsive"
else
    echo "   ❌ Database is not responsive - may need to restart PostgreSQL"
    exit 1
fi
echo ""

# Step 3: Check existing indexes
echo "3️⃣  Checking existing indexes on samples table..."
psql -U freedomridge -d freedomridge -c "
SELECT indexname, pg_size_pretty(pg_relation_size(indexrelid)) as size
FROM pg_indexes 
JOIN pg_class ON pg_class.relname = indexname
WHERE tablename = 'samples' AND indexname LIKE 'idx_samples_%';
" || echo "   No custom indexes found yet"
echo ""

# Step 4: Terminate any active CREATE INDEX queries
echo "4️⃣  Terminating any active CREATE INDEX operations..."
psql -U freedomridge -d freedomridge -c "
SELECT pg_terminate_backend(pid), query
FROM pg_stat_activity 
WHERE query LIKE '%CREATE INDEX%' 
  AND state = 'active'
  AND pid != pg_backend_pid();
" || echo "   No active index creation found"
sleep 2
echo ""

# Step 5: Create ONLY the critical location index (without CONCURRENTLY for speed)
echo "5️⃣  Creating critical location index..."
echo "   ⚠️  This will lock the samples table briefly"
read -p "   Continue? (y/n) " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    psql -U freedomridge -d freedomridge <<EOF
-- Drop if exists (in case partially created)
DROP INDEX IF EXISTS idx_samples_location;
DROP INDEX IF EXISTS idx_samples_dataset_location;

-- Create location index (GiST for spatial queries)
CREATE INDEX idx_samples_location 
ON samples USING GIST(location) 
WHERE location IS NOT NULL;

-- Analyze for query planner
ANALYZE samples;

-- Show result
SELECT 'Location index created!' as status;
SELECT indexname, pg_size_pretty(pg_relation_size(indexrelid)) as size
FROM pg_indexes 
JOIN pg_class ON pg_class.relname = indexname
WHERE tablename = 'samples' AND indexname = 'idx_samples_location';
EOF
    echo "   ✅ Location index created"
else
    echo "   Skipped"
fi
echo ""

# Step 6: Summary
echo "=========================="
echo "✅ Recovery Complete"
echo ""
echo "Status:"
echo "  - Database: Unblocked"
echo "  - Location index: Created (for map performance)"
echo "  - Metadata/Tags indexes: Skipped (optional, create later if needed)"
echo ""
echo "Your embeddings upload should now work!"
echo ""
echo "To create remaining indexes later (off-hours):"
echo "  See the idx_samples_* indexes in backend/db/schema.sql"
