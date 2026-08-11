"""连续闭眼判定的最小自检。"""

from unittest.mock import patch

from detector import FatigueDetector


closed_eye = [{'class': 'closed_eye'}]
open_eye = [{'class': 'open_eye'}]
mixed_eyes = [{'class': 'closed_eye'}, {'class': 'open_eye'}]

with patch('detector.YOLO'):
    detector = FatigueDetector('unused.pt', closed_eye_fatigue_seconds=1.5)

assert detector.analyze_fatigue(closed_eye, now=10.0)[0] == '眨眼'
assert detector.analyze_fatigue(closed_eye, now=11.49)[0] == '眨眼'
assert detector.analyze_fatigue(closed_eye, now=11.5)[0] == '疲劳'
assert detector.analyze_fatigue(open_eye, now=12.0)[0] == '正常'
assert detector.analyze_fatigue(closed_eye, now=12.1)[0] == '眨眼'
assert detector.analyze_fatigue(mixed_eyes, now=14.0)[0] == '正常'

detector.reset_fatigue_state()
assert detector.analyze_fatigue(closed_eye, now=20.0)[0] == '眨眼'
assert detector.analyze_fatigue([], now=20.2)[0] == '检测中...'
assert detector.analyze_fatigue(closed_eye, now=21.5)[0] == '疲劳'

detector.reset_fatigue_state()
assert detector.analyze_fatigue(closed_eye, now=30.0)[0] == '眨眼'
assert detector.analyze_fatigue([], now=30.31)[0] == '检测中...'
assert detector.analyze_fatigue(closed_eye, now=31.5)[0] == '眨眼'

print('fatigue timing self-check passed')
