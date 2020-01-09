import os
import utils
import importlib
import numpy as np
import pandas as pd


def diag_ir(out, calc, factor, tenor_point, aa=None):
    ir = out['Results']['scenarios'][factor]
    size = ir.shape[0]
    perc = np.array([50, 0.50, 2.50, 5.00, 95.00, 97.50, 99.50])
    ten_ir = calc.all_factors[factor].factor.tenors
    all = [pd.DataFrame(np.percentile(i, 100.0 - perc, axis=1), index=perc, columns=ten_ir).T for i in ir]
    comp = pd.DataFrame([x.iloc[ten_ir.searchsorted(tenor_point)] for x in all],
                        index=calc.time_grid.time_grid_years[:size])
    if aa is not None:
        return comp.reindex(comp.index.union(aa['Time (Years)'])).interpolate('index').reindex(aa['Time (Years)'])
    else:
        return comp


def makeflatcurve(curr, bps, tenor=30):
    return {'Currency': curr, 'Curve': utils.Curve([], [[0, bps * 0.01 * 0.01], [tenor, bps * 0.01 * 0.01]]),
            'Day_Count': 'ACT_365', 'Property_Aliases': None, 'Sub_Type': 'None'}


def getpath(pathlist, uat=False):
    for path in pathlist:
        if os.path.isdir(path):
            return os.path.join(path, 'UAT') if uat else path


def set_collateral(cx, Agreement_Currency, Opening_Balance, Received_Threshold=0.0, Posted_Threshold=0.0,
                   Minimum_Received=100000.0, Minimum_Posted=100000.0, Liquidation_Period=10.0):
    cx.deals['Deals']['Children'][0]['instrument'].field.update(
        {'Agreement_Currency': Agreement_Currency, 'Opening_Balance': Opening_Balance,
         'Apply_Closeout_When_Uncollateralized': 'No', 'Collateralized': 'True', 'Settlement_Period': 0.0,
         'Balance_Currency': 'ZAR', 'Liquidation_Period': Liquidation_Period,
         'Credit_Support_Amounts':
             {'Received_Threshold': utils.CreditSupportList({1: Received_Threshold}),
              'Minimum_Received': utils.CreditSupportList({1: Minimum_Received}),
              'Posted_Threshold': utils.CreditSupportList({1: Posted_Threshold}),
              'Minimum_Posted': utils.CreditSupportList({1: Minimum_Posted})
              }
         }
    )


def load_market_data(rundate, path, json_name='MarketData.json', setup_funding=False, cva_default=True):
    from adaptiv import AdaptivContext

    context = AdaptivContext()
    context.parse_json(os.path.join(path, rundate, json_name))
    context.parse_calendar_file(os.path.join(path, 'calendars.cal'))

    context.params['System Parameters']['Base_Date'] = pd.Timestamp(rundate)

    if cva_default:
        context.params['Price Factors']['SurvivalProb.DEFAULT'] = {
            'Recovery_Rate': 0.5,
            'Curve': utils.Curve(
                [], [[0.0, 0.0], [.5, .01], [1, .02], [3, .07], [5, .15], [10, .35], [20, .71], [30, 1.0]]),
            'Property_Aliases': None}

    if setup_funding:
        context.params['Price Factors']['InterestRate.ZAR-SWAP.OIS'] = makeflatcurve('ZAR', -15)
        context.params['Price Factors']['InterestRate.ZAR-SWAP.FUNDING'] = makeflatcurve('ZAR', 10)
        context.params['Price Factors']['InterestRate.USD-LIBOR-3M.FUNDING'] = makeflatcurve('USD', 65)

    return context


def run_cmc(context, rundate, Currency='ZAR', CVA=True, FVA=False, CollVA=False,
            time_grid = '0d 2d 1w(1w) 3m(1m) 2y(3m)'):

    from calculation import construct_calculation

    default_cva = {'Deflate_Stochastically': 'Yes', 'Stochastic_Hazard_Rates': 'No', 'Counterparty': 'DEFAULT'}
    cva_sect = context.deals.get('Calculation', {'Credit_Valuation_Adjustment': default_cva}).get(
        'Credit_Valuation_Adjustment', default_cva)

    params_mc = {'calc_name': ('test1',), 'Time_grid': time_grid, 'Run_Date': rundate,
                 'Currency': Currency, 'Simulation_Batches': 10, 'Batch_Size': 64 * 8, 'Random_Seed': 8312,
                 'Calc_Scenarios': 'No', 'Generate_Cashflows': 'No', 'Partition': 'None',
                 'Generate_Slideshow': 'No', 'PFE_Recon_File': '', 'Dynamic_Scenario_Dates': 'Yes',
                 # 'Debug': 'G:\\Credit Quants\\CRSTAL\\riskflow\\logs', 'NoModel': 'Constant',
                 'Debug': 'No',
                 # 'NoModel':'RiskNeutral',
                 'CVA': {'Deflate_Stochastically': cva_sect['Deflate_Stochastically'],
                         'Stochastic_Hazard': cva_sect['Stochastic_Hazard_Rates'],
                         'Counterparty': cva_sect['Counterparty'], 'Deflation': 'ZAR-SWAP',
                         # brave choices these . . .
                         'Gradient': 'Yes', 'Hessian': 'No'},
                 'FVA': {'Funding_Interest_Curve': 'USD-LIBOR-3M.FUNDING',
                         'Risk_Free_Curve': 'USD-OIS',
                         'Counterparty': cva_sect['Counterparty'],
                         'Stochastic_Funding': 'Yes', },
                 'CollVA': {'Gradient': 'Yes'}
                 }

    if not CVA:
        del params_mc['CVA']
    if not FVA:
        del params_mc['FVA']
    if not CollVA:
        del params_mc['CollVA']

    calc = construct_calculation('Credit_Monte_Carlo', context)
    out = calc.execute(params_mc)
    exposure = out['Results']['mtm'].clip(0.0, np.inf)
    dates = np.array(sorted(calc.time_grid.mtm_dates))[
        calc.netting_sets.sub_structures[0].obj.Time_dep.deal_time_grid]

    res = pd.DataFrame({'EE': np.mean(exposure, axis=1), 'PFE': np.percentile(exposure, 95, axis=1)}, index=dates)

    # useful to compare against adaptiv
    # comp = diag_ir(out, calc, Factor(type='InterestRate', name=('ZAR-SWAP',)), 2, aa=None)
    return calc, out, res


def bootstrap(path, rundate, reuse_cal=True):
    from adaptiv import AdaptivContext

    context = AdaptivContext()

    if reuse_cal and os.path.isfile(os.path.join(path, rundate, 'MarketDataCal.json')):
        context.parse_json(os.path.join(path, rundate, 'MarketDataCal.json'))
    else:
        context.parse_json(os.path.join(path, rundate, 'MarketData.json'))

    context.params['System Parameters']['Base_Date'] = pd.Timestamp(rundate)
    context.parse_calendar_file(os.path.join(path,'calendars.cal'))

    context.bootstrap()
    context.write_marketdata_json(os.path.join(path, rundate, 'MarketDataCal.json'))
    context.write_market_file(os.path.join(path, rundate, 'MarketDataCal.dat'))


if __name__ == '__main__':
    import matplotlib

    matplotlib.use('Qt4Agg')
    import matplotlib.pyplot as plt

    plt.interactive(True)
    # make pandas pretty print
    pd.options.display.float_format = '{:,.5f}'.format

    folder = 'CVA'
    path = getpath(['E:\\Data\\crstal\\{}'.format(folder),
                    'G:\\Credit Quants\\CRSTAL\\{}'.format(folder),
                    'G:\\{}'.format(folder)], uat=False)

    rundate = '2019-12-31'
    cx = load_market_data(rundate, path, json_name='MarketDataCal2.json')
    # bootstrap(path, rundate, reuse_cal=True)
    params_bv = {'calc_name': ('test1',), 'Run_Date': rundate, 'Currency': 'ZAR', 'Greeks': 'No'}
    cx.parse_json(os.path.join(path, rundate, 'CrB_Kathu_Solar_Park_ISDA.json'))
    # calc, out, res = run_cmc(cx, rundate)