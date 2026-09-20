from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple

import geonamescache
import streamlit as st
import swisseph as swe
from timezonefinder import TimezoneFinder
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

# ==================== إعدادات Swiss Ephemeris ====================
swe.set_ephe_path('')  # استخدام وضع Moshier الافتراضي للتقويم الفلكي الدقيق

# ==================== الثوابت وقوائم الكواكب والحكام ====================

TARGET_PLANETS = ['Mars', 'Rahu', 'Ketu', 'Saturn', 'Neptune', 'Uranus', 'Pluto']
SLOW_PLANETS = ['Jupiter', 'Saturn', 'Uranus', 'Neptune', 'Pluto']
HAZARD_CONJUNCT_PLANETS = ['Rahu', 'Ketu', 'Mars', 'Pluto', 'Neptune', 'Uranus', 'Saturn']
ORB = 3.0  # 3 درجات قوسية حسب طلب لجنة التقييم
URANUS_CYCLE_DIVISOR = 4.2857  # معامل التقسيم الخاص بالنظرية (دورة أورانوس ~84 سنة)

PLANET_IDS = {
    'Sun': swe.SUN,
    'Moon': swe.MOON,
    'Mars': swe.MARS,
    'Jupiter': swe.JUPITER,
    'Saturn': swe.SATURN,
    'Uranus': swe.URANUS,
    'Neptune': swe.NEPTUNE,
    'Pluto': swe.PLUTO,
    'Rahu': swe.MEAN_NODE,
    'Ketu': -1,  # يُحسب تلقائياً بإضافة 180 درجة لـ Rahu
}

# جدول حكام الأبراج (التقليدي والحديث) للتحقق من حاكم الطالع
SIGN_RULERS = {
    0: ['Mars'],              # الحمل
    1: ['Venus'],             # الثور
    2: ['Mercury'],           # الجوزاء
    3: ['Moon'],              # السرطان
    4: ['Sun'],               # الأسد
    5: ['Mercury'],           # العذراء
    6: ['Venus'],             # الميزان
    7: ['Mars', 'Pluto'],     # العقرب
    8: ['Jupiter'],           # القوس
    9: ['Saturn'],            # الجدي
    10: ['Saturn', 'Uranus'], # الدلو
    11: ['Jupiter', 'Neptune']# الحوت
}

class City(NamedTuple):
    geonameid: str
    name: str
    country_code: str
    latitude: float
    longitude: float
    admin1: str
    population: int

@dataclass
class USMRow:
    age: float
    cycle_type: str  # أساسي، معارضة (42)، أو عودة (84)
    risk_date: date
    risk_year: float
    trigger_planet: str
    status: str
    active_transits: list[str]
    condition_2_met: bool
    is_priority_planet: bool

# ==================== تحميل البيانات الجغرافية ====================

@st.cache_resource
def load_cities() -> list[City]:
    gc = geonamescache.GeonamesCache()
    cities_list = []
    for city_id, city_data in gc.get_cities().items():
        if city_data.get("population", 0) >= 15000:
            cities_list.append(
                City(
                    geonameid=city_id,
                    name=city_data.get("name", ""),
                    country_code=city_data.get("countrycode", ""),
                    latitude=city_data.get("latitude", 0.0),
                    longitude=city_data.get("longitude", 0.0),
                    admin1=city_data.get("admin1code", ""),
                    population=city_data.get("population", 0),
                )
            )
    return cities_list

@st.cache_resource
def load_countries() -> dict:
    gc = geonamescache.GeonamesCache()
    return gc.get_countries()

def get_cities_by_country(country_code: str) -> list[City]:
    cities = load_cities()
    return sorted(
        [c for c in cities if c.country_code == country_code],
        key=lambda c: c.population,
        reverse=True,
    )

def get_timezone_for_city(city: City) -> str | None:
    try:
        tf = TimezoneFinder()
        return tf.timezone_at(lat=city.latitude, lng=city.longitude)
    except Exception:
        return None

# ==================== الحسابات الفلكية ====================

def get_julian_day(dt_utc: datetime) -> float:
    """تحويل التاريخ والوقت (UTC) إلى التاريخ اليولياني (Julian Day)"""
    decimal_hour = dt_utc.hour + dt_utc.minute / 60.0 + dt_utc.second / 3600.0
    return swe.julday(dt_utc.year, dt_utc.month, dt_utc.day, decimal_hour)

def get_planet_longitude(planet_name: str, dt_utc: datetime) -> float:
    """حساب درجة موقع الكوكب أو النقطة بدقة عالية من 0 الحمل"""
    jd = get_julian_day(dt_utc)
    if planet_name == 'Ketu':
        rahu_pos = swe.calc_ut(jd, swe.MEAN_NODE)[0][0]
        return (rahu_pos + 180.0) % 360.0
    
    planet_id = PLANET_IDS.get(planet_name)
    if planet_id is not None:
        res = swe.calc_ut(jd, planet_id)
        return res[0][0]
    
    raise ValueError(f"اسم الكوكب غير معروف: {planet_name}")

def calculate_placidus_houses(dt_utc: datetime, lat: float, lon: float) -> tuple[float, float, dict[int, float]]:
    """حساب درجات الأوتاد والبيوت الـ 12 بنظام بلاسيديوس (Placidus) باستخدام b'P'"""
    jd = get_julian_day(dt_utc)
    cusps, ascmc = swe.houses(jd, lat, lon, b'P')
    asc_degree = ascmc[0]  # درجة الطالع (House 1 Cusp)
    mc_degree = ascmc[1]   # درجة وسط السماء (House 10 Cusp)
    
    houses = {i + 1: cusps[i] for i in range(12)}
    return asc_degree, mc_degree, houses

def calculate_whole_sign_houses(ascendant_longitude: float) -> dict[int, float]:
    """توزيع البيوت بنظام البرج الكامل في حال عدم معرفة ساعة الميلاد"""
    ascendant_sign = int(ascendant_longitude // 30)
    houses = {}
    for i in range(12):
        house_number = i + 1
        sign_index = (ascendant_sign + i) % 12
        houses[house_number] = sign_index * 30.0
    return houses

def is_within_orb(pos1: float, pos2: float, orb: float = ORB) -> bool:
    """التحقق مما إذا كانت المسافة بين نقطتين قوسيتين ضمن الجرم المسموح (3.0 درجات)"""
    diff = abs(pos1 - pos2)
    diff = min(diff, 360.0 - diff)
    return diff <= orb

def get_sign_zero_degree(deg: float) -> float:
    """إرجاع درجة 0 من البرج الذي تقع فيه النقطة"""
    return math.floor(deg / 30.0) * 30.0

def is_planet_ruler_or_near_asc(planet_name: str, transit_dt: datetime, natal_asc: float, natal_rulers: list[str]) -> bool:
    """
    التحقق مما إذا كان الكوكب العابر (أو أحد الكواكب المقترنة به) 
    حاكماً للطالع الناتال أو قريباً جداً من درجة الطالع بـ orb صغير.
    """
    # 1. التحقق إن كان الكوكب نفسه أو أحد حكام الطالع
    if planet_name in natal_rulers:
        return True
    
    # 2. التحقق من الاقتراب الشديد من درجة الطالع الناتال ضمن الجرم المسموح
    planet_pos = get_planet_longitude(planet_name, transit_dt)
    if is_within_orb(planet_pos, natal_asc, ORB):
        return True
        
    # 3. التحقق من الكواكب المقترنة بهذا الكوكب العابر في نفس وقت العبور
    for h_p in HAZARD_CONJUNCT_PLANETS:
        if h_p == planet_name:
            continue
        h_pos = get_planet_longitude(h_p, transit_dt)
        t_pos = get_planet_longitude(planet_name, transit_dt)
        if is_within_orb(h_pos, t_pos, ORB):
            if h_p in natal_rulers or is_within_orb(h_pos, natal_asc, ORB):
                return True
                
    return False

# ==================== الدالة الرئيسية للتطبيق ====================

def libephemeris():
    st.set_page_config(page_title="نظرية استرو رادار-توقيت الخطر ", layout="wide")
    st.title("نظام نظرية استرو رادار (توقع سنوات الخطر الجسيم)")
    st.caption("تطبيق معايير النظرية الكاملة: نظام بلاسيديوس، نطاق 3 درجات قوسية - الدقة للحدث +/- 6 شهور")
    st.markdown("---")

    # التقاط البلدان والمدن
    countries_data = load_countries()
    country_options = {f"{c['name']} ({code})": code for code, c in countries_data.items()}
    sorted_country_names = sorted(country_options.keys())

    col1, col2, col3 = st.columns(3)

    with col1:
        name = st.text_input("اسم الفرد / الكيان / الدولة", placeholder="أدخل الاسم هنا...")
        selected_country_label = st.selectbox("اختر الدولة", sorted_country_names)
        selected_country_code = country_options[selected_country_label]

    with col2:
        available_cities = get_cities_by_country(selected_country_code)
        if available_cities:
            city_names = [f"{c.name} (تعداد: {c.population:,})" for c in available_cities]
            selected_city_index = st.selectbox("اختر المدينة", range(len(city_names)), format_func=lambda x: city_names[x])
            selected_city = available_cities[selected_city_index]
        else:
            st.error("لم يتم العثور على مدن لهذه الدولة.")
            selected_city = None

        dob = st.date_input("تاريخ الميلاد / الاستقلال", min_value=date(1800, 1, 1), max_value=date(2100, 1, 1), value=date(1990, 1, 1))

    with col3:
        is_time_known = st.checkbox("هل ساعة الميلاد معروفة بدقة؟", value=True)
        if is_time_known:
            # خطوة الوقت 5 دقائق حسب الطلب
            tob = st.time_input("ساعة الميلاد", value=time(12, 0), step=timedelta(minutes=5))
        else:
            tob = time(12, 0)
            st.info("سيتم استخدام البرج الشمسي كبديل للطالع (نظام البرج الكامل).")

        start_year = st.number_input("سنة بداية نطاق التوقع", min_value=1900, max_value=2100, value=2026)
        end_year = start_year + 30
        st.write(f"**نطاق التوقع الممتد:** {start_year} - {end_year} (30 سنة)")

    # زر التنفيذ للتحليل
    if st.button("تشغيل خوارزمية تحليل النظرية", type="primary", use_container_width=True):
        if not name:
            st.warning("يرجى إدخال الاسم قبل بدء التحليل.")
            st.stop()
        if not selected_city:
            st.error("يرجى اختيار مدينة صحيحة.")
            st.stop()

        with st.spinner("جاري معالجة المواضع الفلكية، وتطبيق قواعد الأوتاد، وحساب دورات أورانوس..."):
            
            # ضبط التوقيت العالمي (UTC)
            tz_str = get_timezone_for_city(selected_city)
            if tz_str:
                try:
                    local_tz = ZoneInfo(tz_str)
                    local_dt = datetime.combine(dob, tob).replace(tzinfo=local_tz)
                    utc_dt = local_dt.astimezone(timezone.utc)
                except ZoneInfoNotFoundError:
                    utc_dt = datetime.combine(dob, tob).replace(tzinfo=timezone.utc)
                    tz_str = "UTC (افتراضي)"
            else:
                utc_dt = datetime.combine(dob, tob).replace(tzinfo=timezone.utc)
                tz_str = "UTC (غير محدد)"

            # معلومات جانبية للمراجعة التقنية
            st.sidebar.markdown("### بيانات الخريطة الأساسية")
            st.sidebar.text(f"الكيان: {name}")
            st.sidebar.text(f"المدينة: {selected_city.name}")
            st.sidebar.text(f"الإحداثيات: {selected_city.latitude:.2f}N, {selected_city.longitude:.2f}E")
            st.sidebar.text(f"التوقيت العالمي: {utc_dt.strftime('%Y-%m-%d %H:%M')}")

            # حساب الأوتاد والبيوت
            if is_time_known:
                asc_degree, mc_degree, houses = calculate_placidus_houses(utc_dt, selected_city.latitude, selected_city.longitude)
                house_system_name = "Placidus (نظام بلاسيديوس المتقدم)"
            else:
                asc_degree = get_planet_longitude("Sun", utc_dt)
                mc_degree = (asc_degree + 90.0) % 360.0
                houses = calculate_whole_sign_houses(asc_degree)
                house_system_name = "Solar Sign (نظام البرج الشمسي)"

            # تحديد حاكم الطالع الناتال
            asc_sign_index = int(asc_degree // 30)
            natal_rulers = SIGN_RULERS.get(asc_sign_index, ['Mars'])
            st.sidebar.text(f"برج الطالع: {asc_sign_index} | الحاكم(ون): {', '.join(natal_rulers)}")

            # حساب مواقع كواكب الناتال وتحديد الأقرب إلى 0 الحمل (الأولوية الأولى)
            natal_positions = {}
            distances_from_aries = {}
            for planet in TARGET_PLANETS:
                pos = get_planet_longitude(planet, utc_dt)
                natal_positions[planet] = pos
                distances_from_aries[planet] = pos % 360.0  # المسافة التراكمية من 0 الحمل

            # إيجاد الكوكب الأقرب تماماً لـ 0 الحمل (الأولوية العليا للنظرية)
            priority_planet = min(distances_from_aries, key=distances_from_aries.get)

            st.sidebar.markdown(f"**الكوكب الأقرب لـ 0 الحمل (أولوية قصوى):** `{priority_planet}` (المسافة: {distances_from_aries[priority_planet]:.2f}°)")

            # بناء نقاط الأوتاد المستهدفة بناءً على نص النظرية
            # 1. وتد البيت السادس أو 0 من برجه
            h6_cusp = houses[6]
            h6_targets = [h6_cusp, get_sign_zero_degree(h6_cusp)]

            # 2. وتد البيت الثامن أو 0 من برجه
            h8_cusp = houses[8]
            h8_targets = [h8_cusp, get_sign_zero_degree(h8_cusp)]

            # 3. وتد البيت الثاني عشر أو 10 درجات منه
            h12_cusp = houses[12]
            h12_targets = [h12_cusp, (h12_cusp + 10.0) % 360.0]

            # 4. وتد الطالع (البيت الأول) أو 10 درجات منه
            h1_cusp = houses[1]
            h1_targets = [h1_cusp, (h1_cusp + 10.0) % 360.0]

            usm_results: list[USMRow] = []

            # فحص جميع كواكب الهدف وفق الدورات الثلاث (الأساسية، المعارضة 42، العودة 84)
            for planet, natal_pos in natal_positions.items():
                base_age = distances_from_aries[planet] / URANUS_CYCLE_DIVISOR
                
                # الدورات الموصى بها في النظرية: الأساسية، المعارضة (42 سنة)، العودة (84 سنة)
                cycles_to_check = [
                    (base_age, "دورة أساسية (أورانوس المتحرك)"),
                    (base_age + 42.0, "دورة المعارضة (Opposition - 42 سنة)"),
                    (base_age + 84.0, "دورة العودة (Return - 84 سنة)")
                ]

                is_priority = (planet == priority_planet)

                for target_age, cycle_label in cycles_to_check:
                    days_to_add = target_age * 365.2425
                    target_utc_date = utc_dt + timedelta(days=days_to_add)
                    exact_target_year = target_utc_date.year + (target_utc_date.month - 1) / 12.0

                    # فحص ما إذا كانت السنة تقع ضمن نطاق البحث المطلوب
                    if start_year <= target_utc_date.year <= end_year:
                        
                        condition_1_met = False
                        active_transits = []

                        # فحص الكواكب البطيئة أو الجيلية العابرة
                        for t_planet in SLOW_PLANETS:
                            t_pos = get_planet_longitude(t_planet, target_utc_date)

                            # التحقق من الشرط الأساسي (البيت 6، 8، 12، أو الطالع بدرجاتها المحددة)
                            hit_angle = False
                            
                            # فحص البيت السادس أو 0 منه
                            for target_deg in h6_targets:
                                if is_within_orb(t_pos, target_deg, ORB):
                                    hit_angle = True
                            # فحص البيت الثامن أو 0 منه
                            for target_deg in h8_targets:
                                if is_within_orb(t_pos, target_deg, ORB):
                                    hit_angle = True
                            # فحص البيت الثاني عشر أو 10 منه
                            for target_deg in h12_targets:
                                if is_within_orb(t_pos, target_deg, ORB):
                                    hit_angle = True
                            # فحص الطالع أو 10 منه
                            for target_deg in h1_targets:
                                if is_within_orb(t_pos, target_deg, ORB):
                                    hit_angle = True

                            if hit_angle:
                                condition_1_met = True
                                if t_planet not in active_transits:
                                    active_transits.append(t_planet)

                        # فحص الشرط الإضافي (الاقتران بكواكب الخطر + حاكم الطالع أو قرب شديد من الطالع)
                        condition_2_met = False
                        if condition_1_met:
                            for active_p in active_transits:
                                # 1. هل الكوكب العابر مقترن بأحد كواكب الخطر؟ (أو هو نفسه منها)
                                is_conjunct_hazard = active_p in HAZARD_CONJUNCT_PLANETS
                                if not is_conjunct_hazard:
                                    for h_c in HAZARD_CONJUNCT_PLANETS:
                                        if is_within_orb(get_planet_longitude(active_p, target_utc_date), get_planet_longitude(h_c, target_utc_date), ORB):
                                            is_conjunct_hazard = True
                                            break
                                
                                # 2. هل الكوكب العابر (أو مقترنه) حاكم للطالع أو قريب من درجة الطالع؟
                                is_ruler_or_asc = is_planet_ruler_or_near_asc(active_p, target_utc_date, asc_degree, natal_rulers)

                                if is_conjunct_hazard and is_ruler_or_asc:
                                    condition_2_met = True
                                    break

                        # تسجيل النتائج إذا تحقق الشرط الأساسي على الأقل
                        if condition_1_met:
                            status_text = "خطر بالغ السوء / تحول كارثي (تحقق الشرط الأساسي والإضافي)" if condition_2_met else "تنبيه عبور مهم على الأوتاد (تحقق الشرط الأساسي)"
                            
                            usm_results.append(
                                USMRow(
                                    age=target_age,
                                    cycle_type=cycle_label,
                                    risk_date=target_utc_date.date(),
                                    risk_year=exact_target_year,
                                    trigger_planet=planet,
                                    status=status_text,
                                    active_transits=active_transits,
                                    condition_2_met=condition_2_met,
                                    is_priority_planet=is_priority
                                )
                            )

            # ==================== عرض التقارير والنتائج ====================
            st.subheader(f"تقرير نتائج تحليل النظرية لـ: {name}")
            st.write(f"**الموقع الجغرافي:** {selected_city.name}، {selected_country_label} | **درجة الطالع:** {asc_degree:.2f}° | **النظام المستخدم:** {house_system_name} | **الجرم (Orb):** {ORB}°")

            if not usm_results:
                st.success(f"الوضع الفلكي مستقر ضمن نطاق السنوات ({start_year} - {end_year}): لم يتم رصد تقاطعات تحقق شروط الأوتاد الحساسة بالمعايير الحالية.")
            else:
                # ترتيب النتائج زمنياً
                usm_results.sort(key=lambda x: x.risk_date)
                
                st.warning(f"تم رصد إجمالي ({len(usm_results)}) محطة زمنية حرجة تطابق معايير النظرية في النطاق المحدد:")

                for res in usm_results:
                    priority_badge = " [⭐ كوكب الأولوية القصوى]" if res.is_priority_planet else ""
                    
                    with st.expander(f"تاريخ الخطر: {res.risk_date.strftime('%Y-%m-%d')} | العمر المحسوب: {res.age:.2f} سنة{priority_badge} | الكوكب: {res.trigger_planet}", expanded=res.condition_2_met):
                        st.write(f"**نوع الدورة:** {res.cycle_type}")
                        if res.condition_2_met:
                            st.error(f"**الحالة:** {res.status}")
                        else:
                            st.warning(f"**الحالة:** {res.status}")
                        
                        st.markdown(f"- **الكواكب العابرة على الأوتاد (البيت 6، 8، 12، أو الطالع):** `{', '.join(res.active_transits)}`")
                        st.markdown(f"- **تحقق الشرط الإضافي (حاكم الطالع / الاقتران المدمر):** `{'نعم (مؤشر خطر كارثي عالي)' if res.condition_2_met else 'لا (تنبيه عبور غير مكتمل الأركان الكارثية)'}`")

# تشغيل الدالة في حال تم تنفيذ الملف بشكل مباشر
if __name__ == "__main__":
    libephemeris()