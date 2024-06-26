import numpy as np
from pyamg import test
from fealpy.fem.precomp_data import data

class ScalarMassIntegrator:
    """
    @note (c u, v)
    """    

    def __init__(self, c=None, q=None):
        self.coef = c
        self.q = q
        self.type = 'BL3'

    def assembly_cell_matrix(self, space, index=np.s_[:], cellmeasure=None,
            out=None):
        """
        @note 没有参考单元的组装方式
        """

        q = self.q if self.q is not None else space.p+1
        coef = self.coef
        
        mesh = space.mesh
 
        if cellmeasure is None:
            if mesh.meshtype == 'UniformMesh2d':
                 NC = mesh.number_of_cells()
                 cellmeasure = np.broadcast_to(mesh.entity_measure('cell', index=index), (NC,))
            else:
                cellmeasure = mesh.entity_measure('cell', index=index)

        NC = len(cellmeasure)
        ldof = space.number_of_local_dofs()  

        if out is None:
            M = np.zeros((NC, ldof, ldof), dtype=space.ftype)
        else:
            M = out

        qf = mesh.integrator(q, 'cell')
        bcs, ws = qf.get_quadrature_points_and_weights()

        phi0 = space.basis(bcs, index=index) # (NQ, NC, ldof)
        if coef is None:
            M += np.einsum('q, qci, qcj, c -> cij', ws, phi0, phi0, cellmeasure, optimize=True)
        else:
            if callable(coef):
                if hasattr(coef, 'coordtype'):
                    if coef.coordtype == 'cartesian':
                        ps = mesh.bc_to_point(bcs, index=index)
                        coef = coef(ps)
                    elif coef.coordtype == 'barycentric':
                        coef = coef(bcs, index=index)
                else:
                    ps = mesh.bc_to_point(bcs, index=index)
                    coef = coef(ps)

            if np.isscalar(coef):
                M += coef*np.einsum('q, qci, qcj, c->cij', ws, phi0, phi0, cellmeasure, optimize=True)
            elif isinstance(coef, np.ndarray): 
                if coef.shape == (NC, ):
                    M += np.einsum('q, c, qci, qcj, c -> cij', ws, coef, phi0, phi0, cellmeasure, optimize=True)
                else:
                    M += np.einsum('q, qc, qci, qcj, c -> cij', ws, coef, phi0, phi0, cellmeasure, optimize=True)
            else:
                raise ValueError("coef is not correct!")

        if out is None:
            return M
        
    
    def assembly_cell_matrix_fast(self, space,
            trialspace=None, testspace=None, coefspace=None,
            index=np.s_[:], cellmeasure=None, out=None):
        """
        @brief 基于无数值积分的组装方式
        """
        coef = self.coef

        mesh = space.mesh 
        meshtype = mesh.type

        if trialspace is None:
            trialspace = space
            TAFtype = space.btype
            TAFdegree = space.p
            TAFldof = space.number_of_local_dofs()
        else:
            TAFtype = trialspace.btype
            TAFdegree = trialspace.p
            TAFldof = trialspace.number_of_local_dofs()  

        if testspace is None:
            testspace = trialspace
            TSFtype = TAFtype
            TSFdegree = TAFdegree
            TSFldof = TAFldof
        else:
            TSFtype = testspace.btype
            TSFdegree = testspace.p 
            TSFldof = testspace.number_of_local_dofs()

        if coefspace is None:
            coefspace = testspace
            COFtype = TSFtype
            COFdegree = TSFdegree
            COFldof = TSFldof
        else:
            COFtype = coefspace.btype
            COFdegree = coefspace.p 
            COFldof = coefspace.number_of_local_dofs()

        Itype = self.type 
        dataindex = Itype + "_" + meshtype + "_TAF_" + TAFtype + "_" + \
                str(TAFdegree) + "_TSF_" + TSFtype + "_" + str(TSFdegree)

        if cellmeasure is None:
            if mesh.meshtype == 'UniformMesh2d':
                 NC = mesh.number_of_cells()
                 cellmeasure = np.broadcast_to(mesh.entity_measure('cell', index=index), (NC,))
            else:
                 cellmeasure = mesh.entity_measure('cell', index=index)
        
        NC = len(cellmeasure)

        if out is None:
            M = np.zeros((NC, TSFldof, TAFldof), dtype=trialspace.ftype)
        else:
            M = out

        if coef is None:
            M += np.einsum('c, cij -> cij', cellmeasure, data[dataindex], optimize=True)
        else:
            if callable(coef):
                u = coefspace.interpolate(coef)
                cell2dof = coefspace.cell_to_dof()
                coef = u[cell2dof]
            if np.isscalar(coef):
                M += coef * np.einsum('c, aij -> cij', cellmeasure, data[dataindex], optimize=True)
            elif coef.shape == (NC, COFldof):
                dataindex += "_COF_" + COFtype + "_" + str(COFdegree)
                #print("data[dataindex]:", data[dataindex].shape)
                M += np.einsum('c, ijk, ck -> cij', cellmeasure, data[dataindex], coef, optimize=True)
            elif coef.shape == (NC, ):
                M += np.einsum('c, aij, c -> cij', cellmeasure, data[dataindex], coef, optimize=True)
            else:
                raise ValueError("coef is not correct!")

        if out is None:
            return M
        
    def assembly_cell_matrix_ref(self, space0, _, index=np.s_[:], cellmeasure=None):
        """
        @note 基于参考单元的矩阵组装
        """
        pass
