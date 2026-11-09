// Class-probability overlays (docs/SDK.md "Class probabilities"). A sample's field holds
// {type: 'class_probabilities', probPath, classes: N}: one 8-bit grayscale PNG with the N classes' planes side by
// side (class k in columns k*w to (k+1)*w). A rule turns each pixel's N values into one class; backend/class_rules.py
// checks the same shape when a rule is saved. Class 0 is never drawn, as in index masks.

export interface ClassProbabilities {
    type: 'class_probabilities';
    probPath: string;
    classes: number;
}

export interface ThresholdRule {
    classes: number[];   // the classes whose probabilities are summed
    min: number;         // 0-1: the rule passes at or above it
    class: number;       // the class a passing pixel takes
}

export type ClassRule =
    | { type: 'argmax' }
    | { type: 'rules'; rules: ThresholdRule[]; fallback: number | null };

export const ARGMAX: ClassRule = { type: 'argmax' };

// What a field shows: the class map from a rule, or one class's probability as a heatmap.
export interface ProbabilityView {
    rule: ClassRule;
    show: 'map' | number;
}

/** A pixel's class from its N 8-bit values, or null to draw nothing. Argmax ties go to the lower index. */
export function classOf(values: ArrayLike<number>, rule: ClassRule): number | null {
    let chosen: number | null = null;
    if (rule.type === 'argmax') {
        let best = -1;
        for (let k = 0; k < values.length; k++) {
            if (values[k] > best) {
                best = values[k];
                chosen = k;
            }
        }
    } else {
        chosen = rule.fallback;
        for (const entry of rule.rules) {
            let sum = 0;
            for (const k of entry.classes) sum += values[k] ?? 0;
            if (sum >= entry.min * 255) {
                chosen = entry.class;
                break;
            }
        }
    }
    return chosen === 0 ? null : chosen;
}

/** Each plane's width, or an error when the image can't hold `classes` planes side by side. */
export function planeWidth(imageWidth: number, classes: number): number {
    if (!Number.isInteger(classes) || classes < 2 || imageWidth % classes !== 0) {
        throw new Error(`A probabilities image ${imageWidth} pixels wide doesn't hold ${classes} classes side by side`);
    }
    return imageWidth / classes;
}

/** The N values of one pixel of a decoded image (RGBA, as a canvas returns it). */
export function pixelValues(data: ArrayLike<number>, imageWidth: number, classes: number, x: number, y: number): number[] {
    const w = imageWidth / classes;
    const values: number[] = [];
    for (let k = 0; k < classes; k++) values.push(data[(y * imageWidth + k * w + x) * 4]);
    return values;
}

/** One class per pixel of a decoded image (w × h), -1 where nothing is drawn. */
export function classMap(data: ArrayLike<number>, imageWidth: number, height: number, classes: number, rule: ClassRule): Int16Array {
    const w = planeWidth(imageWidth, classes);
    const out = new Int16Array(w * height);
    const values = new Uint8Array(classes);
    for (let y = 0; y < height; y++) {
        for (let x = 0; x < w; x++) {
            for (let k = 0; k < classes; k++) values[k] = data[(y * imageWidth + k * w + x) * 4];
            const chosen = classOf(values, rule);
            out[y * w + x] = chosen === null ? -1 : chosen;
        }
    }
    return out;
}

/** Whether two rules are the same, whatever order their keys came in (jsonb reorders them). */
export function sameRule(a: ClassRule, b: ClassRule): boolean {
    const canonical = (rule: ClassRule) => rule.type === 'argmax'
        ? 'argmax'
        : JSON.stringify([rule.rules.map(entry => [entry.classes, entry.min, entry.class]), rule.fallback]);
    return canonical(a) === canonical(b);
}

/** A field's default rule from the dataset's segmentation_config, argmax without one. */
export function defaultRule(classRules: Record<string, ClassRule> | undefined, fieldName: string): ClassRule {
    return classRules?.[fieldName] ?? ARGMAX;
}
