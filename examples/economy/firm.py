import math
import random
import asyncio
import numpy as np
from scipy import optimize
from cess.agent import Agent, AgentProxy
from cess.agent.learn import QLearner


class Firm(Agent):
    def __init__(self, labor_cost_per_good, material_cost_per_good, labor_per_equipment, labor_per_worker, supply_increment, profit_increment, wage_increment):
        self._super(Firm, self).__init__(state={
            'desired_supply': 1,
            'desired_equipment': 0,
            'worker_change': 0,
            'workers': [],
            'cash': 50000,
            'revenue': 0,
            'costs': 0,
            'price': 0,
            'profit': 0,
            'prev_profit': 0,
            'leftover': 0,
            'supply': 0,
            'n_sold': 0,
            'profit_margin': 1,
            'equipment': 0,
            'materials': 0,
        })

        self.material_cost_per_good = material_cost_per_good
        self.labor_cost_per_good = labor_cost_per_good
        self.labor_per_equipment = labor_per_equipment
        self.labor_per_worker = labor_per_worker
        self.supply_increment = supply_increment
        self.profit_increment = profit_increment
        self.wage_increment = wage_increment

        # all states map to the same actions
        action_ids = [i for i in range(len(self.actions))]
        states_actions = {s: action_ids for s in range(5)}
        self.learner = QLearner(states_actions, self.reward, discount=0.5, explore=0.1, learning_rate=0.8)

    def pay(self, cost):
        self['cash'] -= cost
        self['costs'] += cost

    @property
    def _production_capacity(self):
        """how many goods can be produced given current labor power"""
        return math.floor(self._labor/self.labor_cost_per_good)

    @property
    def _worker_labor(self):
        """labor from workers, not counting equipment"""
        return self.labor_per_worker * len(self['workers'])

    @property
    def _equipment_labor(self):
        """how much labor can be generated by owned equipment, limited by number of workers
        (one worker is required to operate one piece of equipment)"""
        return min(len(self['workers']), self['equipment']) * self.labor_per_equipment

    @property
    def _labor(self):
        """total productive labor"""
        return self._worker_labor + self._equipment_labor

    def _labor_for_equipment(self, equipment):
        """hypothetical labor that could be produced by some amount of equipment,
        limited by number of workers"""
        return self._worker_labor + min(len(self['workers']), equipment) * self.labor_per_equipment

    @asyncio.coroutine
    def fire(self, worker):
        self['workers'].remove(worker)
        yield from worker.call('quit')

    @asyncio.coroutine
    def hire(self, applicants, wage):
        hired = []
        while self['worker_change'] > 0 and applicants:
            worker = random.choice(applicants)
            employer = yield from worker.get('employer')
            if employer is not None:
                yield from employer.call('fire', worker)
            yield from worker.call('hire', AgentProxy(self), wage)
            applicants.remove(worker)
            self['workers'].append(worker)
            hired.append(worker)
            self['worker_change'] -= 1

        # increase wage to attract more employees
        if self['worker_change'] > 0:
            wage += self.wage_increment
        return hired, self['worker_change'], wage

    @asyncio.coroutine
    def shutdown(self):
        for worker in self['workers']:
            yield from self.fire(worker)

    def produce(self, world):
        """produce the firm's product. the firm will produce the desired supply if possible,
        otherwise, they will produce as much as they can."""

        # limit desired supply to what can be produced given current capacity
        self['supply'] = max(1, min(self['desired_supply'], self._production_capacity))

        # set desired price
        wages = 0
        for w in self['workers']:
            wages += (yield from w.get('wage'))
        self['costs'] += wages
        self['cash'] -= wages
        cost_per_unit = self['costs']/self['supply']
        self['price'] = max(0, cost_per_unit + self['profit_margin'])

        return self['supply'], self['price']

    @asyncio.coroutine
    def sell(self, quantity):
        n_sold = min(self['supply'], quantity)
        self['supply'] -= n_sold
        self['n_sold'] += n_sold
        self['revenue'] = self['price'] * n_sold
        self['cash'] += self['revenue']
        return n_sold

    @property
    def curren(self):
        """represent as a discrete state"""
        if self['n_sold'] == 0:
            return 0
        elif self['n_sold'] > 0 and self['leftover'] > 0:
            return 1
        elif self['n_sold'] > 0 and self['leftover'] == 0 and self['profit'] <= 0:
            return 2
        elif self['n_sold'] > 0 and self['leftover'] == 0 and self['profit'] > 0 and self['profit'] - self['prev_profit'] < 0:
            return 3
        elif self['n_sold'] > 0 and self['leftover'] == 0 and self['profit'] > 0 and self['profit'] - self['prev_profit'] >= 0:
            return 4

    def reward(self, state):
        """the discrete states we map to are the reward values, so just return that"""
        return state

    @property
    def actions(self):
        """these actions are possible from any state"""
        return [
            {'supply': self.supply_increment},
            {'supply': -self.supply_increment},
            {'supply': self.supply_increment, 'profit_margin': self.profit_increment},
            {'supply': self.supply_increment, 'profit_margin': -self.profit_increment},
            {'supply': -self.supply_increment, 'profit_margin': self.profit_increment},
            {'supply': -self.supply_increment, 'profit_margin': -self.profit_increment}
        ]

    def assess_assets(self, required_labor, mean_wage, mean_equip_price):
        """identify desired mixture of productive assets, i.e. workers, equipment, and wage"""
        down_wage_pressure = self.wage_increment

        def objective(x):
            n_workers, wage, n_equipment = x
            return n_workers * wage + n_equipment * mean_equip_price

        def constraint(x):
            n_workers, wage, n_equipment = x
            equip_labor = min(n_workers * self.labor_per_equipment, n_equipment * self.labor_per_equipment)
            return n_workers * self.labor_per_worker + equip_labor - required_labor

        results = optimize.minimize(objective, (1,0,0), constraints=[
            {'type': 'ineq', 'fun': constraint},
            {'type': 'ineq', 'fun': lambda x: x[0]},
            {'type': 'ineq', 'fun': lambda x: x[1] - (mean_wage - down_wage_pressure)},
            {'type': 'ineq', 'fun': lambda x: x[2]}
        ], options={'maxiter':20})
        n_workers, wage, n_equipment = np.ceil(results.x).astype(np.int)
        return n_workers, wage, n_equipment

    @asyncio.coroutine
    def purchase_equipment(self, supplier):
        price, supply = yield from supplier.get('price', 'supply')
        total_equipment_cost = (self['desired_equipment'] - self['equipment']) * price

        if not total_equipment_cost:
            n_equipment = max(0, self['desired_equipment'] - self['equipment'])
        else:
            equipment_budget = max(0, min(self['cash'], total_equipment_cost))

            # how much equipment can be purchased
            n_equipment = math.floor(equipment_budget/price)

        to_purchase = min(supply, n_equipment)
        yield from supplier.call('sell', to_purchase)
        self['equipment'] += to_purchase
        cost = to_purchase * price
        self.pay(cost)
        return self['desired_equipment'] - self['equipment'], to_purchase

    @asyncio.coroutine
    def set_production_target(self, world):
        """firm decides on how much supply they want to produce this step,
        and what they need to do to accomplish that"""

        # assess previous day's results
        self['prev_profit'] = self['profit']
        self['leftover'] = self['supply']

        # adjust production
        action = self.learner.choose_action(self.curren)
        action = self.actions[action]
        self['desired_supply'] = max(1, self['desired_supply'] + action.get('supply', 0))
        self['profit_margin'] += action.get('profit_margin', 0)

        # supply expires every day
        self['supply'] = 0

        # unused materials expire every day
        self['materials'] = 0

        # resets every day
        self['n_sold'] = 0
        self['revenue'] = 0
        self['costs'] = 0

        # figure out labor goal
        required_labor = self['desired_supply'] * self.labor_cost_per_good
        n_workers, wage, n_equip = self.assess_assets(required_labor, world['mean_wage'], world['mean_equip_price'])

        # sometimes optimization function returns a huge negative value for
        # workers, need to look into that further
        n_workers = max(n_workers, 0)
        self['worker_change'] = n_workers - len(self['workers'])
        self['desired_equipment'] = self['equipment'] + max(0, n_equip - self['equipment'])

        # fire workers if necessary
        while self['worker_change'] < 0:
            worker = random.choice(self['workers'])
            yield from self.fire(worker)
            self['worker_change'] += 1

        # job vacancies
        return self['worker_change'], wage


class ConsumerGoodFirm(Firm):
    @property
    def _production_capacity(self):
        return math.floor(min(self._labor/self.labor_cost_per_good, self['materials']/self.material_cost_per_good))

    @asyncio.coroutine
    def purchase_materials(self, supplier):
        # figure out how much can be produced given current labor,
        # assuming the firm buys all the equipment they need
        price, supply = yield from supplier.get('price', 'supply')
        capacity_given_labor = math.floor(self._labor_for_equipment(self['desired_equipment'])/self.labor_cost_per_good)

        # adjust desired production based on labor capacity
        self['desired_supply'] = min(capacity_given_labor, self['desired_supply'])

        # estimate material costs
        required_materials = self.material_cost_per_good * self['desired_supply']
        total_material_cost = (required_materials - self['materials']) * price

        if not total_material_cost:
            n_materials = max(0, required_materials - self['materials'])
        else:
            material_budget = max(0, min(self['cash'], total_material_cost))

            # how many materials can be purchased
            n_materials = math.floor(material_budget/price)

        to_purchase = min(supply, n_materials)
        yield from supplier.call('sell', to_purchase)
        self['materials'] += to_purchase

        cost = to_purchase * price
        self.pay(cost)

        # how many materials are still required
        return required_materials - self['materials'], to_purchase


class CapitalEquipmentFirm(ConsumerGoodFirm):
    pass


class RawMaterialFirm(Firm):
    pass