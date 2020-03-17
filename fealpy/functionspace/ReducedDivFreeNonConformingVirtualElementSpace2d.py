import numpy as np
from numpy.linalg import inv
from scipy.sparse import bmat, coo_matrix, csc_matrix, csr_matrix, spdiags, eye

from .function import Function
from .ScaledMonomialSpace2d import ScaledMonomialSpace2d
from ..quadrature import GaussLegendreQuadrature
from ..quadrature import PolygonMeshIntegralAlg
from ..common import ranges
from ..common import block

class RDFNCVEMDof2d():
    """
    The dof manager of Stokes Div-Free Non Conforming VEM 2d space.
    """
    def __init__(self, mesh, p):
        """

        Parameter
        ---------
        mesh : the polygon mesh
        p : the order the space with p>=2
        """
        self.p = p
        self.mesh = mesh
        # 注意这里只包含每个单元边上的自由度 
        self.cell2dof, self.cell2dofLocation = self.cell_to_dof()

    def boundary_dof(self):
        gdof = self.number_of_global_dofs()
        isBdDof = np.zeros(gdof, dtype=np.bool)
        edge2dof = self.edge_to_dof()
        isBdEdge = self.mesh.ds.boundary_edge_flag()
        isBdDof[edge2dof[isBdEdge]] = True
        return isBdDof

    def edge_to_dof(self):
        p = self.p
        mesh = self.mesh
        NE = mesh.number_of_edges()
        edge2dof = np.arange(NE*p).reshape(NE, p)
        return edge2dof

    def cell_to_dof(self):
        p = self.p
        mesh = self.mesh
        cellLocation = mesh.ds.cellLocation
        cell2edge = mesh.ds.cell_to_edge()

        NC = mesh.number_of_cells()

        ldof = self.number_of_local_dofs()
        cell2dofLocation = np.zeros(NC+1, dtype=np.int)
        cell2dofLocation[1:] = np.add.accumulate(ldof)
        cell2dof = np.zeros(cell2dofLocation[-1], dtype=np.int)

        edge2dof = self.edge_to_dof()
        edge2cell = mesh.ds.edge_to_cell()
        idx = cell2dofLocation[edge2cell[:, [0]]] + edge2cell[:, [2]]*p + np.arange(p)
        cell2dof[idx] = edge2dof

        idx = cell2dofLocation[edge2cell[:, [1]]] + edge2cell[:, [3]]*p + np.arange(p)
        cell2dof[idx] = edge2dof
        return cell2dof, cell2dofLocation

    def number_of_global_dofs(self):
        # 这里只有单元每个边上的自由度
        p = self.p
        mesh = self.mesh
        NE = mesh.number_of_edges()
        NC = mesh.number_of_cells()
        gdof = NE*p
        return gdof

    def number_of_local_dofs(self):
        # 这里只有单元每个边上的自由度,  
        p = self.p
        mesh = self.mesh
        NCE = mesh.number_of_edges_of_cells()
        ldofs = NCE*p
        return ldofs


class ReducedDivFreeNonConformingVirtualElementSpace2d:
    def __init__(self, mesh, p, q=None):
        """
        Parameter
        ---------
        mesh : polygon mesh
        p : the space order, p>=2
        """
        self.p = p
        self.smspace = ScaledMonomialSpace2d(mesh, p, q=q)
        self.mesh = mesh
        self.dof = RDFNCVEMDof2d(mesh, p) # 注意这里只是标量的自由度管理
        self.integralalg = self.smspace.integralalg

        self.CM = self.smspace.cell_mass_matrix()
        self.EM = self.smspace.edge_mass_matrix()

        smldof = self.smspace.number_of_local_dofs()
        ndof = p*(p+1)//2
        self.H0 = inv(self.CM[:, 0:ndof, 0:ndof])
        self.H1 = inv(self.EM[:, 0:p, 0:p])

        self.ftype = self.mesh.ftype
        self.itype = self.mesh.itype

        self.Q, self.L = self.matrix_Q_L()
        self.G, self.B = self.matrix_G_B()
        self.M = self.matrix_M()
        self.R, self.J = self.matrix_R_J()
        self.D = self.matrix_D()
        self.PI0 = self.matrix_PI0()

    def project(self, u):
        p = self.p # p >= 2
        NE = self.mesh.number_of_edges()
        uh = self.function()
        def u0(x):
            return np.einsum('ij..., ijm->ijm...', u(x),
                    self.smspace.edge_basis(x, p=p-1))
        h = self.mesh.entity_measure('edge')
        uh0 = self.integralalg.edge_integral(
                u0, edgetype=True)/h[:, None, None]
        uh[0:2*NE*p] = uh0.T.flat
        if p > 2:
            idx = self.index1(p=p-2) # 一次求导后的非零基函数编号及求导系数
            x = idx['x']
            y = idx['y']
            def u1(x, index):
                return np.einsum('ij..., ijm->ijm...', u(x),
                        self.smspace.basis(x, p=p-2, index=index))
            area = self.smspace.cellmeasure
            uh1 = self.integralalg.integral(u1, celltype=True)/area[:, None, None]
            uh[2*NE*p:] += uh1[:, y[0], 0].flat
            uh[2*NE*p:] -= uh1[:, x[0], 1].flat
        return uh

    def project_to_smspace(self, uh):
        p = self.p
        idof = (p-2)*(p-1)//2
        NE = self.mesh.number_of_edges()
        NC = self.mesh.number_of_cells()
        cell2dof, cell2dofLocation = self.dof.cell2dof, self.dof.cell2dofLocation
        smldof = self.smspace.number_of_local_dofs()
        sh = self.smspace.function(dim=2)
        c2d = self.smspace.cell_to_dof()
        def f(i):
            PI0 = self.PI0[i]
            idx = cell2dof[cell2dofLocation[i]:cell2dofLocation[i+1]]
            x0 = uh[idx]
            x1 = uh[idx+NE*p]
            x2 = np.zeros(idof, dtype=self.ftype)
            if p > 2:
                start = 2*NE*p + i*idof
                x2[:] = uh[start:start+idof]
            x = np.r_[x0, x1, x2]
            y = (PI0@x).flat
            sh[c2d[i], 0] = y[:smldof]
            sh[c2d[i], 1] = y[smldof:2*smldof]
        list(map(f, range(NC)))
        return sh

    def matrix_PI0(self):
        p = self.p
        NC = self.mesh.number_of_cells()
        cell2dof, cell2dofLocation = self.dof.cell2dof, self.dof.cell2dofLocation
        def f(i):
            G = block([
                [self.G[0][i]  ,   self.G[2][i], self.B[0][i]],
                [self.G[2][i].T,   self.G[1][i], self.B[1][i]],
                [self.B[0][i].T, self.B[1][i].T,            0]]
                )
            s = slice(cell2dofLocation[i], cell2dofLocation[i+1])
            R = block([
                [self.R[0][0][:, s], self.R[0][1][:, s], self.R[0][2][i]],
                [self.R[1][0][:, s], self.R[1][1][:, s], self.R[1][2][i]],
                [   self.J[0][:, s],    self.J[1][:, s],     self.J[2][i]]])
            PI = inv(G)@R
            return PI
        PI0 = list(map(f, range(NC)))
        return PI0

    def matrix_Q_L(self):
        p = self.p
        if p > 2:
            mesh = self.mesh
            NC = mesh.number_of_cells()
            area = self.smspace.cellmeasure

            smldof = self.smspace.number_of_local_dofs() # 标量 p 次单元缩放空间的维数
            ndof = (p-2)*(p-1)//2 # 标量 p - 3 次单元缩放单项项式空间的维数
            cell2dof, cell2dofLocation = self.dof.cell2dof, self.dof.cell2dofLocation # 边上的标量的自由度信息
            CM = self.CM

            idx = self.index1(p=p-2)
            x = idx['x']
            y = idx['y']
            Q = CM[:, x[0][:, None], x[0]] + CM[:, y[0][:, None], y[0]]

            L0 = np.zeros((ndof, len(cell2dof)), dtype=self.ftype)
            L1 = np.zeros((ndof, len(cell2dof)), dtype=self.ftype)
            L2 = np.zeros((NC, ndof, ndof), dtype=sel.ftype)
            L2[:, y[0], y[0]] += area[:, None]
            L2[:, x[0], x[0]] += area[:, None] 
            return Q, [L0, L1, L2]
        else:
            return None, None

    def matrix_G_B(self):
        """
        计算单元投投影算子的左端矩阵
        """
        p = self.p
        mesh = self.mesh
        NC = mesh.number_of_cells()

        smldof = self.smspace.number_of_local_dofs()
        ndof = smldof - p - 1

        area = self.smspace.cellmeasure
        ch = self.smspace.cellsize
        CM = self.CM
        
        smldof = self.smspace.number_of_local_dofs()
        ndof = smldof - p - 1
        # 分块矩阵 G = [[G00, G01], [G10, G11]]
        G00 = np.zeros((NC, smldof, smldof), dtype=self.ftype)
        G01 = np.zeros((NC, smldof, smldof), dtype=self.ftype)
        G11 = np.zeros((NC, smldof, smldof), dtype=self.ftype)

        idx = self.smspace.index1()
        x = idx['x']
        y = idx['y']
        L = x[1][None, ...]/ch[..., None]
        R = y[1][None, ...]/ch[..., None]
        mxx = np.einsum('ij, ijk, ik->ijk', L, CM[:, 0:ndof, 0:ndof], L)
        myx = np.einsum('ij, ijk, ik->ijk', R, CM[:, 0:ndof, 0:ndof], L)
        myy = np.einsum('ij, ijk, ik->ijk', R, CM[:, 0:ndof, 0:ndof], R)

        G00[:, x[0][:, None], x[0]] += mxx
        G00[:, y[0][:, None], y[0]] += 0.5*myy

        G01[:, y[0][:, None], x[0]] += 0.5*myx

        G11[:, x[0][:, None], x[0]] += 0.5*mxx
        G11[:, y[0][:, None], y[0]] += myy

        mx = L*CM[:, 0, 0:ndof]/area[:, None]
        my = R*CM[:, 0, 0:ndof]/area[:, None]
        G00[:, y[0][:, None], y[0]] += np.einsum('ij, ik->ijk', my, my)
        G01[:, y[0][:, None], x[0]] -= np.einsum('ij, ik->ijk', my, mx)
        G11[:, x[0][:, None], x[0]] += np.einsum('ij, ik->ijk', mx, mx)

        m = CM[:, 0, :]/area[:, None]
        val = np.einsum('ij, ik->ijk', m, m)
        G00[:, 0:smldof, 0:smldof] += val
        G11[:, 0:smldof, 0:smldof] += val
        G = [G00, G11, G01]

        # 分块矩阵 B = [B0, B1]
        B0 = np.zeros((NC, smldof, ndof), dtype=self.ftype)
        B1 = np.zeros((NC, smldof, ndof), dtype=self.ftype)
        B0[:, x[0]] = np.einsum('ij, ijk->ijk', L, CM[:, 0:ndof, 0:ndof])
        B1[:, y[0]] = np.einsum('ij, ijk->ijk', R, CM[:, 0:ndof, 0:ndof])

        B = [B0, B1]
        return G, B

    def matrix_M(self):
        """
        计算 M_{k-2} 和 \\tilde\\bPhi 的矩阵, 这是其它右端矩阵计算的基础。
        """
        p = self.p

        mesh = self.mesh
        NC = mesh.number_of_cells()

        area = self.smspace.cellmeasure # 单元面积
        ch = self.smspace.cellsize # 单元尺寸 

        smldof = self.smspace.number_of_local_dofs(p=p-2) # 标量 p-2 次单元缩放空间的维数
        idof = (p-2)*(p-1)//2
        cell2dof, cell2dofLocation = self.dof.cell2dof, self.dof.cell2dofLocation # 标量的单元边自由度信息

        M00 = np.zeros((smldof, len(cell2dof)), dtype=self.ftype) 
        M01 = np.zeros((smldof, len(cell2dof)), dtype=self.ftype) 

        M10 = np.zeros((smldof, len(cell2dof)), dtype=self.ftype) 
        M11 = np.zeros((smldof, len(cell2dof)), dtype=self.ftype) 

        M02 = np.zeros((NC, smldof, idof), dtype=self.ftype) # 单元内部自由度
        M12 = np.zeros((NC, smldof, idof), dtype=self.ftype) 
        if p > 2:
            idx = self.smspace.index1(p=p-2)
            x = idx['x']
            y = idx['y']
            c = np.repeat(range(1, p), range(1, p))
            idx = np.arange(idof)
            M02[:, y[0], idx] = area[:, None]*y[1]/c[y[0]]
            M12[:, x[0], idx] = -area[:, None]*x[1]/c[x[0]]

        node = mesh.entity('node')
        edge = mesh.entity('edge')
        n = mesh.edge_unit_normal()
        eh = mesh.entity_measure('edge')
        edge2cell = mesh.ds.edge_to_cell()
        isInEdge = (edge2cell[:, 0] != edge2cell[:, 1])

        # self.H1 is the inverse of Q_{p-1}^F
        ndof = self.smspace.number_of_local_dofs(p=p-1)
        qf = GaussLegendreQuadrature(p + 3)
        bcs, ws = qf.quadpts, qf.weights
        ps = np.einsum('ij, kjm->ikm', bcs, node[edge])
        phi = self.smspace.edge_basis(ps, p=p-1)

        phi0 = self.smspace.basis(ps, index=edge2cell[:, 0], p=p-1)
        m0 = self.H0[:, 0, 0:ndof]/area[:, None]
        phi0 -= m0[None, edge2cell[:, 0], :]
        c = 1/np.repeat(range(1, p), range(1, p))
        F0 = np.einsum('i, ijm, ijn, j, j, j->jmn', 
                ws, phi0, phi, eh, eh, ch[edge2cell[:, 0]])@self.H1
        F0 /= c[None, :, None]

        idx = self.smspace.index1(p=p-1)
        x = idx['x']
        y = idx['y']
        idx0 = cell2dofLocation[edge2cell[:, [0]]] + edge2cell[:, [2]]*p + np.arange(p)
        val = np.einsum('ijk, i->jik', F0, n[:, 0])
        np.add.at(M00, (np.s_[:], idx0), val[x[0], ...])
        np.add.at(M10, (np.s_[:], idx0), val[y[0], ...])

        val = np.einsum('ijk, i->jik', F0, n[:, 1])
        np.add.at(M01, (np.s_[:], idx0), val[x[0], ...])
        np.add.at(M11, (np.s_[:], idx0), val[y[0], ...])

        if isInEdge.sum() > 0:
            phi1 = self.smspace.basis(ps, index=edge2cell[:, 1], p=p-1)
            phi1 -= m0[None, edge2cell[:, 1], :]
            F1 = np.einsum('i, ijm, ijn, j->jmn', 
                    ws, phi1, phi, eh, eh, ch[edge2cell[:, 1]])@self.H1
            F1 /= c[None, :, None]
            idx0 = cell2dofLocation[edge2cell[:, [1]]] + edge2cell[:, [3]]*p + np.arange(p)
            val = np.einsum('ijk, i->jik', F1, n[:, 0])
            np.subtract.at(M00, (np.s_[:], idx0[isInEdge]), val[x[0], isInEdge])
            np.subtract.at(M10, (np.s_[:], idx0[isInEdge]), val[y[0], isInEdge])

            val = np.einsum('ijk, i->jik', F1, n[:, 1])
            np.subtract.at(M01, (np.s_[:], idx0[isInEdge]), val[x[0], isInEdge])
            np.subtract.at(M11, (np.s_[:], idx0[isInEdge]), val[y[0], isInEdge])
            
        return [[M00, M01, M02], [M10, M11, M12]]

    def matrix_R_J(self):
        """
        计算单元投投影算子的右端矩阵
        """
        p = self.p

        mesh = self.mesh
        NC = mesh.number_of_cells()

        area = self.smspace.cellmeasure # 单元面积
        ch = self.smspace.cellsize # 单元尺寸 

        smldof = self.smspace.number_of_local_dofs() # 标量 p 次单元缩放空间的维数
        ndof = smldof - p - 1 # 标量 p - 1 次单元缩放单项项式空间的维数
        cell2dof, cell2dofLocation = self.dof.cell2dof, self.dof.cell2dofLocation # 标量的单元边自由度信息

        CM = self.CM # 单元质量矩阵

        # 构造分块矩阵 R = [[R00, R01, R02], [R10, R11, R12]]
        idof = (p-2)*(p-1)//2
        R00 = np.zeros((smldof, len(cell2dof)), dtype=self.ftype)
        R01 = np.zeros((smldof, len(cell2dof)), dtype=self.ftype)
        R02 = np.zeros((NC, smldof, idof), dtype=self.ftype)

        R10 = np.zeros((smldof, len(cell2dof)), dtype=self.ftype)
        R11 = np.zeros((smldof, len(cell2dof)), dtype=self.ftype)
        R12 = np.zeros((NC, smldof, idof), dtype=self.ftype)

        idx = self.smspace.index2()
        xx = idx['xx']
        yy = idx['yy']
        xy = idx['xy']

        ldofs = self.dof.number_of_local_dofs()
        a = np.repeat(area, ldofs)
        R00[xx[0], :] -=     xx[1][:, None]*self.M[0][0]
        R00[yy[0], :] -= 0.5*yy[1][:, None]*self.M[0][0]
        R00[xy[0], :] -= 0.5*xy[1][:, None]*self.M[1][0]
        R00 /= a

        R01[xx[0], :] -=     xx[1][:, None]*self.M[0][1]
        R01[yy[0], :] -= 0.5*yy[1][:, None]*self.M[0][1]
        R01[xy[0], :] -= 0.5*xy[1][:, None]*self.M[1][1]
        R01 /= a

        if p > 2:
            R02[:, xx[0], :] -=     xx[1][None, :, None]*self.M[0][2]
            R02[:, yy[0], :] -= 0.5*yy[1][None, :, None]*self.M[0][2]
            R02[:, xy[0], :] -= 0.5*xy[1][None, :, None]*self.M[1][2]
            R02 /= area[:, None, None]

        R10[xx[0], :] -= 0.5*xx[1][:, None]*self.M[1][0]
        R10[yy[0], :] -=     yy[1][:, None]*self.M[1][0]
        R10[xy[0], :] -= 0.5*xy[1][:, None]*self.M[0][0]
        R10 /= a

        R11[xx[0], :] -= 0.5*xx[1][:, None]*self.M[1][1]
        R11[yy[0], :] -=     yy[1][:, None]*self.M[1][1]
        R11[xy[0], :] -= 0.5*xy[1][:, None]*self.M[0][1]
        R11 /= a

        if p > 2:
            R12[:, xx[0], :] -= 0.5*xx[1][None, :, None]*self.M[1][2]
            R12[:, yy[0], :] -=     yy[1][None, :, None]*self.M[1][2]
            R12[:, xy[0], :] -= 0.5*xy[1][None, :, None]*self.M[0][2]
            R12 /= area[:, None, None]
            
        node = mesh.entity('node')
        edge = mesh.entity('edge')
        n = mesh.edge_unit_normal()
        eh = mesh.entity_measure('edge')
        edge2cell = mesh.ds.edge_to_cell()
        isInEdge = (edge2cell[:, 0] != edge2cell[:, 1])

        # self.H1 is the inverse of Q_{p-1}^F
        qf = GaussLegendreQuadrature(p + 3)
        bcs, ws = qf.quadpts, qf.weights
        ps = np.einsum('ij, kjm->ikm', bcs, node[edge])
        phi0 = self.smspace.basis(ps, index=edge2cell[:, 0], p=p-1)
        phi = self.smspace.edge_basis(ps, p=p-1)
        F0 = np.einsum('i, ijm, ijn, j, j->jmn', ws, phi0, phi, eh, eh)@self.H1

        idx = self.smspace.index1() # 一次求导后的非零基函数编号及求导系数
        x = idx['x']
        y = idx['y']
        idx0 = cell2dofLocation[edge2cell[:, [0]]] + edge2cell[:, [2]]*p + np.arange(p)
        h2 = x[1].reshape(1, -1)/ch[edge2cell[:, [0]]]
        h3 = y[1].reshape(1, -1)/ch[edge2cell[:, [0]]]

        val = np.einsum('ij, ijk, i->jik', h2, F0, n[:, 0])
        np.add.at(R00, (x[0][:, None, None], idx0), val)
        val = np.einsum('ij, ijk, i->jik', h3, F0, 0.5*n[:, 1])
        np.add.at(R00, (y[0][:, None, None], idx0), val)

        val = np.einsum('ij, ijk, i->jik', h2, F0, 0.5*n[:, 0])
        np.add.at(R11, (x[0][:, None, None], idx0), val)
        val = np.einsum('ij, ijk, i->jik', h3, F0, n[:, 1])
        np.add.at(R11, (y[0][:, None, None], idx0), val)

        val = np.einsum('ij, ijk, i->jik', h3, F0, 0.5*n[:, 0])
        np.add.at(R01, (y[0][:, None, None], idx0), val)
        val = np.einsum('ij, ijk, i->jik', h2, F0, 0.5*n[:, 1])
        np.add.at(R10, (x[0][:, None, None], idx0), val)

        a2 = area**2
        start = cell2dofLocation[edge2cell[:, 0]] + edge2cell[:, 2]*p
        val = np.einsum('ij, ij, i, i->ji',
            h3, CM[edge2cell[:, 0], 0:ndof, 0], eh/a2[edge2cell[:, 0]], n[:, 1])
        np.add.at(R00, (y[0][:, None], start), val)

        val = np.einsum('ij, ij, i, i->ji',
            h2, CM[edge2cell[:, 0], 0:ndof, 0], eh/a2[edge2cell[:, 0]], n[:, 0])
        np.add.at(R11, (x[0][:, None], start), val)

        val = np.einsum('ij, ij, i, i->ji',
            h3, CM[edge2cell[:, 0], 0:ndof, 0], eh/a2[edge2cell[:, 0]], n[:, 0])
        np.subtract.at(R01, (y[0][:, None], start), val)

        val = np.einsum('ij, ij, i, i->ji',
            h2, CM[edge2cell[:, 0], 0:ndof, 0], eh/a2[edge2cell[:, 0]], n[:, 1])
        np.subtract.at(R10, (x[0][:, None], start), val)

        if isInEdge.sum() > 0:
            phi1 = self.smspace.basis(ps, index=edge2cell[:, 1], p=p-1)
            F1 = np.einsum('i, ijm, ijn, j, j->jmn', ws, phi1, phi, eh, eh)@self.H1
            idx0 = cell2dofLocation[edge2cell[:, [1]]] + edge2cell[:, [3]]*p + np.arange(p)
            h2 = x[1].reshape(1, -1)/ch[edge2cell[:, [1]]]
            h3 = y[1].reshape(1, -1)/ch[edge2cell[:, [1]]]

            val = np.einsum('ij, ijk, i->jik', h2, F1[:, 0:ndof], n[:, 0])
            np.subtract.at(R00, (x[0][:, None, None], idx0[isInEdge]), val[:, isInEdge])
            val = np.einsum('ij, ijk, i->jik', h3, F1[:, 0:ndof], 0.5*n[:, 1])
            np.subtract.at(R00, (y[0][:, None, None], idx0[isInEdge]), val[:, isInEdge])

            val = np.einsum('ij, ijk, i->jik', h2, F1[:, 0:ndof], 0.5*n[:, 0])
            np.subtract.at(R11, (x[0][:, None, None], idx0[isInEdge]), val[:, isInEdge])
            val = np.einsum('ij, ijk, i->jik', h3, F1[:, 0:ndof], n[:, 1])
            np.subtract.at(R11, (y[0][:, None, None], idx0[isInEdge]), val[:, isInEdge])

            val = np.einsum('ij, ijk, i->jik', h3, F1[:, 0:ndof], 0.5*n[:, 0])
            np.subtract.at(R01, (y[0][:, None, None], idx0[isInEdge]), val[:, isInEdge])
            val = np.einsum('ij, ijk, i->jik', h2, F1[:, 0:ndof], 0.5*n[:, 1])
            np.subtract.at(R10, (x[0][:, None, None], idx0[isInEdge]), val[:, isInEdge])

            start = cell2dofLocation[edge2cell[:, 1]] + edge2cell[:, 3]*p
            val = np.einsum('ij, ij, i, i->ji',
                h3, CM[edge2cell[:, 1], 0:ndof, 0], eh/a2[edge2cell[:, 1]], n[:, 1])
            np.subtract.at(R00, (y[0][:, None], start[isInEdge]), val[:, isInEdge])

            val = np.einsum('ij, ij, i, i->ji',
                h2, CM[edge2cell[:, 1], 0:ndof, 0], eh/a2[edge2cell[:, 1]], n[:, 0])
            np.subtract.at(R11, (x[0][:, None], start[isInEdge]), val[:, isInEdge])

            val = np.einsum('ij, ij, i, i->ji',
                h3, CM[edge2cell[:, 1], 0:ndof, 0], eh/a2[edge2cell[:, 1]], n[:, 0])
            np.add.at(R01, (y[0][:, None], start[isInEdge]), val[:, isInEdge])

            val = np.einsum('ij, ij, i, i->ji',
                h2, CM[edge2cell[:, 1], 0:ndof, 0], eh/a2[edge2cell[:, 1]], n[:, 1])
            np.add.at(R10, (x[0][:, None], start[isInEdge]), val[:, isInEdge])

        val = CM[:, :, 0].T/area[None, :]**2
        def f0(i):
            s = slice(cell2dofLocation[i], cell2dofLocation[i+1])
            R00[:, s] += val[:, i, None]*self.M[0][0][0, s] 
            R11[:, s] += val[:, i, None]*self.M[1][1][0, s] 
        list(map(f0, range(NC)))
        if p > 2:
            R02 += np.einsum('ij, jm->jim', val, self.M[0][2][:, 0, :]) 
            R12 += np.einsum('ij, jm->jim', val, self.M[1][2][:, 0, :])
        
        R = [[R00, R01, R02], [R10, R11, R12]]

        # 分块矩阵 J =[J0, J1, J2, J3, J2+J3]
        idx= self.smspace.index1(p=p-1)
        x = idx['x']
        y = idx['y']
        J0 = np.zeros((ndof, len(cell2dof)), dtype=self.ftype)
        J1 = np.zeros((ndof, len(cell2dof)), dtype=self.ftype)
        ldofs = self.dof.number_of_local_dofs()
        h = np.repeat(ch, ldofs)
        J0[x[0], :] -= x[1][:, None]*self.M[0][0]/h
        J0[y[0], :] -= y[1][:, None]*self.M[1][0]/h
        J1[x[0], :] -= x[1][:, None]*self.M[0][1]/h
        J1[y[0], :] -= y[1][:, None]*self.M[1][1]/h

        J2 = np.zeros((NC, ndof, idof), dtype=self.ftype)
        if p > 2:
            J2[:, x[0], :] -= x[1][None, :, None]*self.M[0][2]/ch[:, None, None]
            J2[:, y[0], :] -= y[1][None, :, None]*self.M[1][2]/ch[:, None, None]

        idx0 = cell2dofLocation[edge2cell[:, [0]]] + edge2cell[:, [2]]*p + np.arange(p)
        val = np.einsum('ijk, i->jik', F0, n[:, 0])
        np.add.at(J0, (np.s_[:], idx0), val)
        val = np.einsum('ijk, i->jik', F0, n[:, 1])
        np.add.at(J1, (np.s_[:], idx0), val)

        if isInEdge.sum() > 0:
            idx0 = cell2dofLocation[edge2cell[:, [1]]] + edge2cell[:, [3]]*p + np.arange(p)
            val = np.einsum('ijk, i->jik', F1, n[:, 0])
            np.subtract.at(J0, (np.s_[:], idx0[isInEdge]), val[:, isInEdge])
            val = np.einsum('ijk, i->jik', F1, n[:, 1])
            np.subtract.at(J1, (np.s_[:], idx0[isInEdge]), val[:, isInEdge])

        J = [J0, J1, J2]
        return R, J

    def matrix_D(self):
        p = self.p

        mesh = self.mesh
        NC = mesh.number_of_cells()
        NE = mesh.number_of_edges()
        NV = mesh.number_of_vertices_of_cells()
        edge = mesh.entity('edge')
        node = mesh.entity('node')
        edge2cell = mesh.ds.edge_to_cell()

        area = self.smspace.cellmeasure # 单元面积
        ch = self.smspace.cellsize # 单元尺寸 
        CM = self.CM

        smldof = self.smspace.number_of_local_dofs() # 标量 p 次单元缩放空间的维数
        ndof = (p-1)*p//2
        idof = (p-2)*(p-1)//2

        cell2dof = self.dof.cell2dof 
        cell2dofLocation = self.dof.cell2dofLocation # 标量的自由度信息

        D0 = np.zeros((len(cell2dof), smldof), dtype=self.ftype)
        D1 = np.zeros((NC, idof, smldof), dtype=self.ftype)
        D2 = np.zeros((NC, idof, smldof), dtype=self.ftype)

        qf = GaussLegendreQuadrature(p + 3)
        bcs, ws = qf.quadpts, qf.weights
        ps = np.einsum('ij, kjm->ikm', bcs, node[edge])
        phi0 = self.smspace.basis(ps, index=edge2cell[:, 0])
        phi = self.smspace.edge_basis(ps, p=p-1)
        F0 = self.H1@np.einsum('i, ijm, ijn->jmn', ws, phi, phi0)
        idx0 = cell2dofLocation[edge2cell[:, [0]]] + edge2cell[:, [2]]*p + np.arange(p)
        np.add.at(D0, (idx0, np.s_[:]), F0)

        isInEdge = (edge2cell[:, 0] != edge2cell[:, 1])
        if isInEdge.sum() > 0:
            phi1 = self.smspace.basis(ps, index=edge2cell[:, 1])
            F1 = self.H1@np.einsum('i, ijm, ijn->jmn', ws, phi, phi1)
            idx1 = cell2dofLocation[edge2cell[:, [1]]] + edge2cell[:, [3]]*p + np.arange(p)
            np.add.at(D0, (idx1[isInEdge], np.s_[:]), F1[isInEdge])

        if p > 2:
            idx = self.smspace.index1(p=p-2) # 一次求导后的非零基函数编号及求导系数
            x = idx['x']
            y = idx['y']
            D1 =  CM[:, y[0], :]/area[:, None, None] # check here
            D2 = -CM[:, x[0], :]/area[:, None, None] # check here

        return D0, D1, D2

    def matrix_A(self):

        p = self.p
        idof = (p-2)*(p-1)//2
        cell2dof, cell2dofLocation = self.dof.cell2dof, self.dof.cell2dofLocation
        NC = self.mesh.number_of_cells()

        mesh = self.mesh
        NE = mesh.number_of_edges()
        edge2cell = mesh.ds.edge_to_cell()
        isInEdge = (edge2cell[:, 0] != edge2cell[:, 1])
        eh = mesh.entity_measure('edge')
        area = self.smspace.cellmeasure
        ch = self.smspace.cellsize

        ldof = self.dof.number_of_local_dofs() # 这里只包含边上的自由度
        f = lambda ndof: np.zeros((ndof, ndof), dtype=self.ftype)
        S00 = list(map(f, ldof))
        S11 = list(map(f, ldof))
        S01 = list(map(f, ldof))
        S22 = np.zeros((NC, idof, idof), dtype=self.ftype) 

        def f1(i):
            j = edge2cell[i, 2]
            c = eh[i]**2/ch[edge2cell[i, 0]]
            S00[edge2cell[i, 0]][p*j:p*(j+1), p*j:p*(j+1)] += self.H1[i]*c
            S11[edge2cell[i, 0]][p*j:p*(j+1), p*j:p*(j+1)] += self.H1[i]*c
        def f2(i):
            if isInEdge[i]:
                j = edge2cell[i, 3]
                c = eh[i]**2/ch[edge2cell[i, 1]]
                S00[edge2cell[i, 1]][p*j:p*(j+1), p*j:p*(j+1)] += self.H1[i]*c
                S11[edge2cell[i, 1]][p*j:p*(j+1), p*j:p*(j+1)] += self.H1[i]*c
        list(map(f1, range(NE)))
        list(map(f2, range(NE)))

        if p > 2:
            L = self.L[2]
            Q = self.Q
            S22 = L.swapaxes(-2, -1)@inv(Q)@L

        ndof = p*(p+1)//2
        Z = np.zeros((ndof, ndof), dtype=self.ftype)
        ldof = self.dof.number_of_local_dofs()
        def f4(i):
            s = slice(cell2dofLocation[i], cell2dofLocation[i+1])
            PI0 = self.PI0[i]
            D = np.eye(PI0.shape[1])
            D0 = self.D[0][s, :]
            D1 = self.D[1][i]
            D2 = self.D[2][i]
            D -= block([
                [D0,  0],
                [ 0, D0],
                [D1, D2]])@PI0
            A = D.T@block([
                [  S00[i], S01[i],      0], 
                [S01[i].T, S11[i],      0],
                [       0,      0, S22[i]]])@D
            J0 = self.J[0][:, s]
            J1 = self.J[1][:, s]
            J2 = self.J[2][i]
            J3 = self.J[3][i]
            U = block([
                [J0,  0,    J2],
                [J1, J0, J2+J3],
                [ 0, J1,    J3]])
            H0 = block([
                [self.H0[i],              0,          0],
                [         0, 0.5*self.H0[i],          0],
                [         0,              0, self.H0[i]]])
            return A + U.T@H0@U 

        A = list(map(f4, range(NC)))
        def f5(i):
            s = slice(cell2dofLocation[i], cell2dofLocation[i+1])
            cd = np.r_[cell2dof[s], NE*p + cell2dof[s], 2*NE*p + np.arange(i*idof, (i+1)*idof)]
            return np.meshgrid(cd, cd)
        idx = list(map(f5, range(NC)))
        I = np.concatenate(list(map(lambda x: x[1].flat, idx)))
        J = np.concatenate(list(map(lambda x: x[0].flat, idx)))

        gdof = self.number_of_global_dofs() 

        A = np.concatenate(list(map(lambda x: x[0].flat, A)))
        A = csr_matrix((A, (I, J)), shape=(gdof, gdof), dtype=self.ftype)
        return  

    def matrix_P(self):
        p = self.p
        idof = (p-2)*(p-1)//2
        cell2dof, cell2dofLocation = self.dof.cell2dof, self.dof.cell2dofLocation
        NC = self.mesh.number_of_cells()
        cd1 = self.smspace.cell_to_dof(p=p-1)
        def f0(i):
            s = slice(cell2dofLocation[i], cell2dofLocation[i+1])
            cd = np.r_[cell2dof[s], NE*p + cell2dof[s], 2*NE*p + np.arange(i*idof, (i+1)*idof)]
            return np.meshgrid(cd, cd1[i])
        idx = list(map(f0, range(NC)))
        I = np.concatenate(list(map(lambda x: x[1].flat, idx)))
        J = np.concatenate(list(map(lambda x: x[0].flat, idx)))

        def f(i, k):
            J = self.J[k][cell2dofLocation[i]:cell2dofLocation[i+1]]
            return J.flatten()
        gdof0 = self.smspace.number_of_global_dofs(p=p-1)
        gdof1 = self.dof.number_of_global_dofs()

        P0 = np.concatenate(list(map(lambda i: f(i, 0), range(NC))))
        P0 = coo_matrix((P0, (I, J)),
                shape=(gdof0, gdof1), dtype=self.ftype)

        P1 = np.concatenate(list(map(lambda i: f(i, 1), range(NC))))
        P1 = coo_matrix((P1, (I, J)),
                shape=(gdof0, gdof1), dtype=self.ftype)
        if p == 2:
            return bmat([[P0, P1]], format='csr')
        else:
            P2 = np.concatenate(list(map(lambda i: f(i, 4), range(NC))))
            P2 = coo_matrix((P1, (I, J)),
                    shape=(gdof0, NC*idof), dtype=self.ftype)
            return bmat([[P0, P1, P2]], format='csr')
            

    def source_vector(self, f):
        p = self.p
        ndof = self.smspace.number_of_local_dofs(p=p-2)
        Q = inv(self.CM[:, :ndof, :ndof])
        phi = lambda x: self.smspace.basis(x, p=p-2)
        def u(x, index):
            return np.einsum('ij..., ijm->ijm...', f(x),
                    self.smspace.basis(x, index=index, p=p-2))
        bb = self.integralalg.integral(u, celltype=True)
        bb = Q@bb
        bb *= self.smspace.cellmeasure[:, None, None]
        gdof = self.number_of_global_dofs()
        cell2dof = self.cell_to_dof(doftype='cell')
        b = np.zeros(gdof, dtype=self.ftype)
        b[cell2dof, :] = bb
        return b

    def set_dirichlet_bc(self, gh, g, is_dirichlet_edge=None):
        """
        """
        p = self.p
        mesh = self.mesh
        isBdEdge = mesh.ds.boundary_edge_flag()
        edge2dof = self.dof.edge_to_dof()

        qf = GaussLegendreQuadrature(self.p + 3)
        bcs, ws = qf.quadpts, qf.weights
        ps = mesh.edge_bc_to_point(bcs, index=isBdEdge)
        val = g(ps)

        ephi = self.smspace.edge_basis(ps, index=isBdEdge, p=p-1)
        b = np.einsum('i, ij..., ijk->jk...', ws, val, ephi)
        gh[edge2dof[isBdEdge]] = self.H1[isBdEdge]@b

    def number_of_global_dofs(self):
        mesh = self.mesh
        NE = mesh.number_of_edges()
        NC = mesh.number_of_cells()
        p = self.p
        gdof = 2*NE*p
        if p > 2:
            gdof += NC*(p-2)*(p-1)//2
        return gdof

    def number_of_local_dofs(self):
        p = self.p
        mesh = self.mesh
        NCE = mesh.number_of_edges_of_cells()
        ldofs = 2*NCE*p
        if p > 2:
            ldofs += (p-2)*(p-1)//2
        return ldofs

    def cell_to_dof(self, doftype='all'):
        if doftype is 'all':
            return self.dof.cell2dof, self.dof.cell2dofLocation
        elif doftype is 'cell':
            p = self.p
            NE = self.mesh.number_of_edges()
            NC = self.mesh.number_of_cells()
            idof = (p-2)*(p-1)//2
            cell2dof = 2*NE*p + np.arange(NC*idof).reshape(NC, idof)
            return cell2dof
        elif doftype is 'edge':
            return self.dof.edge_to_dof()

    def boundary_dof(self):
        return self.dof.boundary_dof()

    def function(self, dim=None, array=None):
        f = Function(self, dim=dim, array=array)
        return f

    def array(self, dim=None):
        gdof = self.number_of_global_dofs()
        if dim in {None, 1}:
            shape = gdof
        elif type(dim) is int:
            shape = (gdof, dim)
        elif type(dim) is tuple:
            shape = (gdof, ) + dim
        return np.zeros(shape, dtype=self.ftype)