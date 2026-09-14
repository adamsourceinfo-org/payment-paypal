-- 方案可以帶免費試用：PayPal 那邊是在 REGULAR 週期前面多放一個 TRIAL 週期。
-- 這裡只存週數，不存 PayPal 的 billing_cycles 原文 —— caller 要看的是「試用幾週」，
-- 而 PayPal 的方案建了就不能改，所以這個數字建了也不會變。
-- 舊資料補 0（沒有試用），跟它們當初建的時候一樣。
-- 加欄位帶預設值是相容變更：跳過 migration 的實例照 app/db.py 的前提照常服務。
ALTER TABLE plans ADD COLUMN IF NOT EXISTS trial_weeks integer NOT NULL DEFAULT 0;
