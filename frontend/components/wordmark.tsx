/**
 * The product wordmark. The name is Freedom Ridge; what everyone calls it is FRIDGE — F + RIDGE.
 * "reedom" sits dimmed so FRIDGE reads straight out of the full name, and when the enclosing
 * `group` is hovered it folds away and leaves just FRIDGE — and the light comes on: frost at rest,
 * lit cyan with a glow on hover.
 */
export function Wordmark({ className = '' }: { className?: string }) {
    return (
        <span className={`inline-grid uppercase tracking-wider ${className}`}>
            <span className="sr-only">Freedom Ridge</span>
            {/* Holds the full width, so the hover target doesn't shrink out from under the cursor as
                the name folds (which would un-hover it and flicker). */}
            <span aria-hidden="true" className="invisible col-start-1 row-start-1 whitespace-pre">
                Freedom Ridge
            </span>
            <span
                aria-hidden="true"
                className="col-start-1 row-start-1 whitespace-pre text-cyan-100 transition-[color,text-shadow] duration-500 ease-in-out group-hover:text-cyan-400 group-hover:[text-shadow:0_0_.25em_rgb(34_211_238/.75),0_0_.7em_rgb(34_211_238/.45)] motion-reduce:transition-none"
            >
                F
                <span className="inline-block max-w-[6em] overflow-hidden align-bottom text-slate-500 transition-[max-width,opacity] duration-500 ease-in-out group-hover:max-w-0 group-hover:opacity-0 motion-reduce:transition-none">
                    {'reedom '}
                </span>
                Ridge
            </span>
        </span>
    );
}
